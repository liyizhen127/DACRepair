"""Shared experiment orchestration. Existing scoring and environment code is unchanged.

An agent plugin implements generate(task, environment) -> unified_diff_text.
Alternatively, consume final predictions in instance_id/model_patch JSONL format.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time

from .core import Benchmark

HEADERS = ["instance_id", "status"]
NON_RETRYABLE = {"PASS", "FAIL"}  # Original statuses; ERROR can be explicitly retried.


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_results(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != HEADERS:
            raise ValueError(f"CSV requires exactly these two columns: {HEADERS}")
        rows = list(reader)
    ids = [row[HEADERS[0]] for row in rows]
    if not ids or any(not iid for iid in ids) or len(ids) != len(set(ids)):
        raise ValueError("CSV must contain distinct, nonempty sample IDs")
    if any(set(row) != set(HEADERS) or row[HEADERS[1]] not in ("", "PASS", "FAIL") for row in rows):
        raise ValueError("CSV results must be blank, PASS or FAIL")
    return ids, {row[HEADERS[0]]: row[HEADERS[1]] for row in rows}


def recorded_result(status):
    """Binary experiment result; preserve the original status in the receipt."""
    if status is None:
        return ""
    if status == "PASS":
        return "PASS"
    if status in ("FAIL", "ERROR"):
        return "FAIL"
    raise ValueError(f"Invalid original evaluation status: {status}")


def write_results(path, ids, records):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(HEADERS)
    for iid in ids:
        status = records.get(iid, {}).get("status")
        writer.writerow([iid, recorded_result(status)])
    atomic_write(path, stream.getvalue())


@contextmanager
def exclusive_lock(path):
    import fcntl  # Linux, WSL and macOS; native Windows is not the target runtime.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process is using this result CSV") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def load_predictions(path, known_ids, follow=False):
    if not path.exists():
        if follow:
            return {}
        raise ValueError(f"Prediction file does not exist: {path}")
    lines = path.read_bytes().splitlines(keepends=True)
    if follow and lines and not lines[-1].endswith(b"\n"):
        lines.pop()  # Wait until the producer has appended the complete JSONL row.
    predictions = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"Invalid prediction JSON at line {number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Prediction at line {number} must be an object")
        iid = row.get("instance_id")
        if not isinstance(iid, str) or iid not in known_ids or not isinstance(row.get("model_patch"), str):
            raise ValueError(f"Unknown ID or missing string model_patch at line {number}")
        if iid in predictions and predictions[iid] != row:
            raise ValueError(f"Conflicting final predictions for {iid}")
        predictions[iid] = row
    return predictions


def summarize(ids, records):
    counts = Counter(records[iid]["status"] for iid in ids if iid in records)
    return {"total": len(ids), "PASS": counts["PASS"], "FAIL": counts["FAIL"] + counts["ERROR"],
            "pending": len(ids) - sum(counts.values()), "complete": sum(counts.values()) == len(ids),
            "raw_status_counts": {status: counts[status] for status in ("PASS", "FAIL", "ERROR")}}


def resolve_agent(spec):
    module_name, separator, name = spec.partition(":")
    if not separator or not module_name.startswith("baseline.") or not name.isidentifier():
        raise ValueError("Agent entry must be baseline.<agent>.<module>:<function>")
    function = getattr(importlib.import_module(module_name), name)
    if not callable(function):
        raise ValueError("Agent entry is not callable")
    return function


class ExperimentRunner:
    def __init__(self, benchmark, results, protocol, *, require_verified=False):
        self.benchmark = benchmark
        self.results = Path(results).resolve()
        self.state_path = self.results.with_suffix(".state.json")
        self.lock_path = self.results.with_suffix(".lock")
        self.protocol = {**protocol, "resources": benchmark.resources.to_dict(),
                         "require_verified": require_verified}
        self.require_verified = require_verified

    def run(self, *, predictions=None, agent=None, follow=False, poll=10, limit=0,
            retry_errors=False, sleep=time.sleep):
        if (predictions is None) == (agent is None):
            raise ValueError("Supply exactly one of predictions or agent")
        if follow and predictions is None:
            raise ValueError("follow is only supported for prediction files")
        if not math.isfinite(poll) or poll <= 0 or limit < 0:
            raise ValueError("poll must be positive and finite; limit must be nonnegative")
        with exclusive_lock(self.lock_path):
            ids, csv_values = read_results(self.results)
            known = {p.stem for p in (self.benchmark.root / "confirmed_fixed").glob("*.json")}
            if not set(ids) <= known:
                raise ValueError("CSV contains sample IDs absent from confirmed_fixed")
            protocol = {**self.protocol, "input_mode": "agent" if agent else "predictions"}
            state = read_json(self.state_path) if self.state_path.exists() else {
                "protocol": protocol, "instance_ids": ids, "records": {}}
            if state["protocol"] != protocol or state["instance_ids"] != ids:
                raise ValueError("Experiment settings or assignment changed; use a separate result set")
            records = state["records"]
            if not set(records) <= set(ids):
                raise ValueError("State contains samples outside the CSV assignment")
            cases = {iid: self.benchmark.load(iid) for iid in ids}
            fingerprints = {iid: case.fingerprint for iid, case in cases.items()}
            for iid, record in records.items():
                if record["status"] not in ("PASS", "FAIL", "ERROR"):
                    raise ValueError(f"Invalid recorded status for {iid}")
                if record["fingerprint"] != fingerprints[iid]:
                    raise ValueError(f"Evaluation fingerprint changed for {iid}")
            for iid, value in csv_values.items():
                if value and value != recorded_result(records.get(iid, {}).get("status")):
                    raise ValueError(f"CSV contains a result without a matching evaluation receipt: {iid}")
            run_key = sha(json.dumps({"protocol": protocol, "ids": ids}, sort_keys=True).encode())[:24]
            work = self.benchmark.root / "runs/experiments" / run_key
            save_json(self.state_path, state)
            write_results(self.results, ids, records)  # Recover receipt-before-CSV interruptions.
            attempted = set()
            processed = 0
            while True:
                available = load_predictions(Path(predictions), known, follow) if predictions is not None else None
                for iid in ids:
                    if available is not None and iid not in available:
                        continue
                    proposed = available[iid]["model_patch"] if available is not None else None
                    previous = records.get(iid)
                    if previous and proposed is not None and previous.get("candidate_sha256") not in (
                            None, sha(proposed.encode())):
                        raise ValueError(f"Final patch changed for {iid}; refusing to replace the evaluated candidate")
                    if previous and (previous["status"] in NON_RETRYABLE or not retry_errors):
                        continue
                    if iid in attempted:
                        continue
                    attempted.add(iid)
                    case = cases[iid]
                    fingerprint = fingerprints[iid]
                    candidate_path = work / iid / "candidate.json"
                    patch = None
                    stage = "generation" if agent else "prediction"
                    started = time.monotonic()
                    print(json.dumps({"event": "START", "instance_id": iid, "stage": stage}), flush=True)
                    try:
                        if candidate_path.exists():
                            candidate = read_json(candidate_path)
                            patch = candidate["model_patch"]
                            if (candidate["fingerprint"] != fingerprint or
                                    sha(patch.encode()) != candidate["candidate_sha256"]):
                                raise ValueError(f"Prepared candidate is stale or damaged for {iid}")
                            if proposed is not None and patch != proposed:
                                raise ValueError(f"Final patch differs from the prepared candidate for {iid}")
                        elif proposed is not None:
                            patch = proposed
                        elif previous and previous.get("candidate_sha256") is not None:
                            raise ValueError("Prepared candidate is missing; restore runs/ before retrying")
                        else:
                            record = case._record
                            task = {key: record[key] for key in (
                                "instance_id", "repo", "base_commit", "problem_statement")}
                            with case.create_repair_env(require_verified=self.require_verified) as environment:
                                patch = agent(task, environment)
                            if not isinstance(patch, str):
                                raise ValueError("Agent must return unified diff text (a string)")
                        if previous and previous.get("candidate_sha256") not in (None, sha(patch.encode())):
                            raise ValueError("Candidate hash differs from the previous attempt")
                        save_json(candidate_path, {"model_patch": patch,
                            "candidate_sha256": sha(patch.encode()), "fingerprint": fingerprint})
                        # Save the final candidate before evaluating, so an interruption
                        # does not trigger another model call or another candidate choice.
                        stage = "evaluation"
                        result = case.evaluate(patch, require_verified=self.require_verified).to_dict()
                    except Exception as exc:
                        result = {"instance_id": iid, "status": "ERROR",
                                  "reason": f"{type(exc).__name__}: {exc}"}
                    if result.get("status") not in ("PASS", "FAIL", "ERROR"):
                        raise ValueError("Evaluator returned an invalid status")
                    receipt = {"status": result["status"], "stage": stage,
                        "candidate_sha256": (previous["candidate_sha256"] if previous and previous.get("candidate_sha256")
                                             else sha(patch.encode()) if isinstance(patch, str) else None),
                        "fingerprint": fingerprint, "evaluation_result": result,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "seconds": round(time.monotonic() - started, 3)}
                    save_json(work / iid / "result.json", receipt)
                    records[iid] = receipt
                    save_json(self.state_path, state)
                    write_results(self.results, ids, records)
                    processed += 1
                    print(json.dumps({"event": "RESULT", "instance_id": iid,
                        "status": recorded_result(result["status"]), "raw_status": result["status"],
                        "reason": result.get("reason"),
                        **summarize(ids, records)}, ensure_ascii=False), flush=True)
                    if limit and processed >= limit:
                        return summarize(ids, records)
                summary = summarize(ids, records)
                if not follow or summary["pending"] == 0:
                    return summary
                sleep(poll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "status"):
        p = sub.add_parser(command)
        p.add_argument("--results", type=Path, required=True, help="Assigned two-column result CSV")
        if command == "run":
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument("--predictions", type=Path)
            source.add_argument("--agent", help="baseline.<name>.<module>:generate")
            p.add_argument("--run-id", required=True, help="Experiment name shared across devices/batches; include model details here")
            p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
            p.add_argument("--jobs", type=int, default=12)
            p.add_argument("--cpus", type=float, default=12)
            p.add_argument("--memory", default="28g")
            p.add_argument("--follow", action="store_true")
            p.add_argument("--poll", type=float, default=10)
            p.add_argument("--limit", type=int, default=0)
            p.add_argument("--retry-errors", action="store_true")
            p.add_argument("--require-verified", action="store_true")
    args = parser.parse_args()
    if args.command == "status":
        ids, values = read_results(args.results)
        state_path = args.results.with_suffix(".state.json")
        state = read_json(state_path) if state_path.exists() else {"records": {}, "instance_ids": ids}
        if state["instance_ids"] != ids:
            raise ValueError("State and CSV assignments differ")
        records = state["records"]
        for iid, value in values.items():
            if value and value != recorded_result(records.get(iid, {}).get("status")):
                raise ValueError(f"CSV result lacks a matching receipt: {iid}")
        output = summarize(ids, records)
    else:
        benchmark = Benchmark(args.root, jobs=args.jobs, cpus=args.cpus, memory=args.memory)
        protocol = {"run_id": args.run_id, "agent_entry": args.agent}
        runner = ExperimentRunner(benchmark, args.results, protocol, require_verified=args.require_verified)
        output = runner.run(predictions=args.predictions, agent=resolve_agent(args.agent) if args.agent else None,
            follow=args.follow, poll=args.poll, limit=args.limit, retry_errors=args.retry_errors)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 2 if output["raw_status_counts"]["ERROR"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped. Re-run the same command to resume from saved results/candidates.")
        raise SystemExit(130)
    except (ValueError, OSError, KeyError, ImportError, AttributeError) as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
