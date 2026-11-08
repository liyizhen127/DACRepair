"""One CLI for evaluation and offline maintenance; no automatic display exports."""
import argparse
import collections
import json
from pathlib import Path

from .core import Benchmark
from .case_tools import DATA_FIELDS, unpack_tests, update_case
from .util import now


def inventory(benchmark):
    rows = []
    for case in benchmark.cases():
        ready = case.readiness
        spec = case.manifest['evaluation_spec']
        rows.append({'instance_id': case.instance_id, 'engine': case.manifest['environment_spec']['engine'],
                     'test_origin': spec['test_origin'], 'test_status': spec.get('test_status'),
                     **{k: ready[k] for k in ('ready', 'blockers', 'execution_ready', 'execution_blockers', 'runtime_verified')}})
    return {'updated_at': now(), 'fixed_total': len(rows), 'ready': sum(r['ready'] for r in rows),
            'not_ready': sum(not r['ready'] for r in rows),
            'ready_definition': 'test material present, including explicitly blocked placeholders; not runtime proof',
            'execution_ready': sum(r['execution_ready'] for r in rows),
            'runtime_verified': sum(r['runtime_verified'] for r in rows),
            'tests_by_origin': dict(collections.Counter(r['test_origin'] for r in rows)),
            'tests_by_status': dict(collections.Counter(r['test_status'] for r in rows)),
            'execution_blockers': dict(collections.Counter(b for r in rows for b in r['execution_blockers'])),
            'by_database': {engine: {'total': sum(r['engine'] == engine for r in rows),
                                     'ready': sum(r['engine'] == engine and r['ready'] for r in rows)}
                            for engine in sorted({r['engine'] for r in rows})}}


def main():
    ap = argparse.ArgumentParser(description='Single-file fixed benchmark; evaluation exit 0/1/2 = PASS/FAIL/ERROR')
    ap.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--jobs', type=int, default=12, help='Parallel build jobs (default: 12)')
    ap.add_argument('--cpus', type=float, default=12, help='CPU quota per running container (default: 12)')
    ap.add_argument('--memory', default='28g', help='Memory limit per running container (default: 28g)')
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('inventory', help='Print live statistics without writing summary/index files')
    sub.add_parser('check-data', help='Check all sample JSONs and embedded test assets; no Docker/tests')
    p = sub.add_parser('inspect'); p.add_argument('instance_id')
    p = sub.add_parser('evaluate'); p.add_argument('instance_id'); p.add_argument('--patch', required=True)
    p.add_argument('--require-verified', action='store_true')
    p = sub.add_parser('validate-reference'); p.add_argument('instance_ids', nargs='+')
    p = sub.add_parser('unpack-tests', help='Restore generated test files for editing without executing them')
    p.add_argument('instance_id'); p.add_argument('--output', required=True)
    p = sub.add_parser('update-case', help='Update one sample and record a reversible change receipt')
    p.add_argument('instance_id')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--test-dir'); group.add_argument('--test-patch')
    p.add_argument('--problem-statement')
    p.add_argument('--config', help='JSON with environment_spec, evaluation_spec and/or blockers replacements')
    p.add_argument('--source-url', action='append', default=[], help='Archive a source URL in the OldData update receipt')
    p = sub.add_parser('export-jsonl', help='Export on demand; no persistent duplicate is required')
    p.add_argument('--output', required=True)
    p.add_argument('--swe-fields-only', action='store_true', help='Export the 6 retained core data fields only')
    # Accept resource flags both before and after execution/inspection commands.
    for name in ('evaluate', 'validate-reference', 'inspect', 'inventory', 'check-data'):
        p = sub.choices[name]
        p.add_argument('--jobs', type=int, default=argparse.SUPPRESS, help='Parallel build jobs')
        p.add_argument('--cpus', type=float, default=argparse.SUPPRESS, help='CPU quota per running container')
        p.add_argument('--memory', default=argparse.SUPPRESS, help='Memory limit per running container')
    args = ap.parse_args()
    try:
        benchmark = Benchmark(args.root, jobs=args.jobs, cpus=args.cpus, memory=args.memory)
        if args.command in {'inventory', 'check-data'}:
            value = inventory(benchmark)
            if args.command == 'check-data':
                value = {'status': 'OK', 'fixed_total': value['fixed_total'], 'ready': value['ready'],
                         'execution_ready': value['execution_ready'], 'runtime_verified': value['runtime_verified'],
                         'docker_runs': 0, 'sample_tests_executed': 0}
        elif args.command == 'inspect':
            case = benchmark.load(args.instance_id)
            value = {'manifest': case.manifest, 'readiness': case.readiness,
                     'runtime_resources': case.resources.to_dict()}
        elif args.command == 'unpack-tests':
            value = unpack_tests(benchmark.load(args.instance_id)._record, args.output)
        elif args.command == 'update-case':
            if not any((args.test_dir, args.test_patch, args.problem_statement, args.config, args.source_url)):
                ap.error('update-case requires at least one update option')
            value = update_case(benchmark.root, args.instance_id, test_dir=args.test_dir, test_patch=args.test_patch,
                                problem=args.problem_statement,
                                config=args.config, source_urls=args.source_url)
        elif args.command == 'export-jsonl':
            output = Path(args.output).resolve()
            if output.suffix != '.jsonl' or output.is_relative_to(benchmark.root / 'confirmed_unfixed') or output.is_relative_to(benchmark.root / 'confirmed_fixed'):
                raise ValueError('Export to a new .jsonl file outside the canonical record directories')
            records = [case._record for case in benchmark.cases()]
            if args.swe_fields_only:
                records = [{k: r[k] for k in DATA_FIELDS} for r in records]
            with output.open('x', encoding='utf-8') as stream:
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            value = {'records': len(records), 'output': str(output), 'swe_fields_only': args.swe_fields_only}
        elif args.command == 'evaluate':
            result = benchmark.load(args.instance_id).evaluate(Path(args.patch).read_bytes().decode('utf-8'),
                                                                require_verified=args.require_verified)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return result.exit_code
        else:
            code = 0
            for iid in args.instance_ids:
                print(json.dumps({'event': 'VALIDATION_START', 'instance_id': iid}), flush=True)
                result = benchmark.load(iid).validate_reference()
                print(json.dumps({'event': 'VALIDATION_END', 'instance_id': iid, 'status': result['status']}), flush=True)
                if result['status'] != 'VALIDATED':
                    code = 2 if result['status'] == 'EVALUATION_ERROR' else max(code, 1)
            return code
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'ERROR', 'reason': str(exc)}, ensure_ascii=False))
        return 2
