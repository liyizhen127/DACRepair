#!/usr/bin/env python3
"""Trusted evaluator inside a fresh container; exit 0/1/2 = PASS/FAIL/ERROR."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

OUTDIR = Path('/tmp/acrepair-evaluation')
OUTDIR.mkdir(exist_ok=True)
PLAN = json.loads(Path('/checks/plan.json').read_text())
RESULT = {'instance_id': PLAN['instance_id'], 'status': 'ERROR', 'reason': 'NOT_FINISHED',
          'patch_applied': False, 'build_succeeded': False, 'required_tests_executed': False,
          'checks': [], 'evidence': {'observed_bug': False, 'stages': []}}


def finish(status, reason):
    RESULT.update(status=status, reason=reason)
    RESULT['exit_code'] = {'PASS': 0, 'FAIL': 1, 'ERROR': 2}[status]
    (OUTDIR / 'result.json').write_text(json.dumps(RESULT, ensure_ascii=False, indent=2) + '\n')
    print('BENCHMARK_RESULT=' + json.dumps(RESULT), flush=True)
    return RESULT['exit_code']


def run(name, argv, cwd='/src', timeout=1800, env=None):
    started = time.monotonic()
    log = OUTDIR / (name + '.log')
    with log.open('w') as stream:
        stream.write(json.dumps({'argv': argv, 'cwd': cwd}) + '\n')
        stream.flush()
        try:
            p = subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, env=env)
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = 124
            stream.write('\nEVALUATOR_TIMEOUT\n')
    RESULT['evidence']['stages'].append({'name': name, 'exit_code': code,
        'seconds': round(time.monotonic() - started, 3), 'log': log.name})
    return code, log.read_text(errors='replace')


def focused(script, prefix):
    # Shared helpers are evaluator-owned; case assets remain next to the script.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
    code, text = run('focused', ['python3', script], timeout=900, env=env)
    lines = [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
    if code or len(lines) != 1:
        raise RuntimeError('FOCUSED_RUNNER_ERROR: nonzero exit or missing/duplicate structured result')
    obs = json.loads(lines[0])
    if obs.get('harness_error'):
        raise RuntimeError(obs['harness_error'])
    if 'checks' in obs:
        checks = obs['checks']
    else:
        checks = [{**c, 'kind': 'target'} for c in obs['targets']] + [{**c, 'kind': 'control'} for c in obs['controls']]
    expected = PLAN['evaluation'].get('required_check_ids', [])
    names = [c.get('name') for c in checks]
    if not checks or not expected or len(names) != len(set(names)) or not set(expected).issubset(names):
        raise RuntimeError('REQUIRED_CHECKS_NOT_EXECUTED')
    if any(type(c.get('passed')) is not bool for c in checks):
        raise RuntimeError('INVALID_CHECK_RESULTS')
    RESULT['checks'].extend(checks)
    RESULT['evidence']['focused'] = obs
    controls = [c for c in checks if c.get('kind') == 'control']
    RESULT['evidence']['observed_bug'] = bool(obs.get('reproduced')) and all(c['passed'] for c in controls)
    # Some case-local scripts also execute their touched upstream Tcl test.
    upstream = obs.get('upstream_test', {})
    if PLAN['evaluation'].get('native_inside_focused'):
        count = re.search(r'(\d+) errors out of (\d+) tests', text)
        if not count or int(count[2]) <= 0 or 'exit_code' not in upstream:
            raise RuntimeError('UPSTREAM_TESTS_NOT_EXECUTED')
        RESULT['checks'].append({'name': 'upstream_tcl_suite', 'kind': 'target',
                                 'passed': upstream['exit_code'] == 0 and int(count[1]) == 0,
                                 'total': int(count[2]), 'errors': int(count[1])})
    return all(c['passed'] for c in RESULT['checks'])


def native_postgres():
    code, text = run('upstream', ['make', 'check'], cwd='/build/src/test/regress', timeout=1800)
    found = {}
    for line in text.splitlines():
        m = re.match(r'^\s*(not ok|ok)\s+\d+\s+(?:[-+]\s*)?(\w+)\s+', line)
        if m:
            found[m[2]] = m[1] == 'ok' and '# SKIP' not in line.upper()
            continue
        m = re.match(r'^\s*(?:test\s+)?(\w+)\s+\.\.\.\s+(ok|FAILED)', line)
        if m:
            found[m[1]] = m[2] == 'ok'
    expected = PLAN['evaluation']['native_tests']
    if not expected or not found or not set(expected).issubset(found):
        raise RuntimeError('REQUIRED_NATIVE_TESTS_NOT_EXECUTED')
    summary = re.search(r'(?:All (\d+) tests passed|\b\d+ of (\d+) tests failed)', text)
    if not summary or int(summary[1] or summary[2]) != len(found):
        raise RuntimeError('NATIVE_SUITE_INCOMPLETE')
    diff = Path('/build/src/test/regress/regression.diffs')
    diff_text = diff.read_text(errors='replace') if diff.exists() else ''
    if diff.exists():
        shutil.copy2(diff, OUTDIR / 'regression.diffs')
    for name, passed in found.items():
        RESULT['checks'].append({'name': 'upstream:' + name, 'kind': 'target' if name in expected else 'control', 'passed': passed})
    target_fail = any(not found[t] and t + '.out' in diff_text for t in expected)
    controls_ok = all(p for n, p in found.items() if n not in expected)
    markers = PLAN['evaluation'].get('baseline_diff_contains', [])
    specific_failure = bool(markers) and all(m in diff_text for m in markers)
    RESULT['evidence']['native_observed_bug'] = code != 0 and target_fail and controls_ok and specific_failure
    RESULT['evidence']['observed_bug'] |= RESULT['evidence']['native_observed_bug']
    if code == 0 and not all(found.values()):
        raise RuntimeError('INCONSISTENT_NATIVE_EXIT_CODE')
    if code != 0 and all(found.values()):
        raise RuntimeError('NATIVE_INFRASTRUCTURE_ERROR')
    return code == 0 and all(found.values())


def native_sqlite():
    good = True
    for index, test in enumerate(PLAN['evaluation']['native_tests']):
        code, text = run(f'upstream_{index}', ['/build/testfixture', '/src/' + test], cwd='/build', timeout=900)
        count = re.search(r'(\d+) errors out of (\d+) tests', text)
        if not count or int(count[2]) <= 0:
            raise RuntimeError('SQLITE_TEST_CRASH_OR_INCOMPLETE')
        passed = code == 0 and int(count[1]) == 0
        RESULT['checks'].append({'name': 'upstream:' + test, 'kind': 'target', 'passed': passed,
                                 'total': int(count[2]), 'errors': int(count[1])})
        RESULT['evidence']['observed_bug'] |= code != 0 and int(count[1]) > 0
        good &= passed
    return good


def main():
    spec = PLAN['evaluation']
    if spec.get('test_status') == 'dependency_placeholder':
        return finish('ERROR', 'DEPENDENCY_BLOCKED')
    candidate = Path('/checks/candidate.patch')
    if candidate.read_text().strip():
        code, _ = run('candidate_check', ['git', 'apply', '--check', str(candidate)])
        if code:
            return finish('FAIL', 'PATCH_APPLY_FAILED')
        code, _ = run('candidate_apply', ['git', 'apply', str(candidate)])
        if code:
            return finish('FAIL', 'PATCH_APPLY_FAILED')
    RESULT['patch_applied'] = True
    tests = Path('/checks/test_patch.diff')
    if tests.read_text().strip():
        code, _ = run('test_patch_check', ['git', 'apply', '--check', str(tests)])
        if code:
            return finish('ERROR', 'TRUSTED_TEST_PATCH_CONFLICT')
        code, _ = run('test_patch_apply', ['git', 'apply', str(tests)])
        if code:
            return finish('ERROR', 'TRUSTED_TEST_PATCH_APPLY_FAILED')
    code, _ = run('build', ['bash', '/opt/bench/build.sh', 'rebuild'], cwd='/build', timeout=5400)
    if code == 124:
        return finish('ERROR', 'BUILD_TIMEOUT')
    if code:
        return finish('FAIL', 'BUILD_FAILED')
    RESULT['build_succeeded'] = True
    passed = True
    if spec.get('focused') == 'generated':
        entrypoint = spec.get('test_entrypoint')
        if not entrypoint:
            raise RuntimeError('PACKAGED_TEST_ENTRYPOINT_MISSING_OR_INVALID')
        script = (Path('/src') / entrypoint).resolve()
        if not script.is_relative_to(Path('/src')) or not script.is_file():
            raise RuntimeError('PACKAGED_TEST_ENTRYPOINT_MISSING_OR_INVALID')
        passed &= focused(str(script), 'CHECK_RESULT=')
    elif spec.get('focused') is not None:
        raise RuntimeError('UNSUPPORTED_FOCUSED_MODE')
    if spec.get('native_tests') and not spec.get('native_inside_focused'):
        if PLAN['engine'] == 'postgres':
            passed &= native_postgres()
        elif PLAN['engine'] == 'sqlite':
            passed &= native_sqlite()
        else:
            raise RuntimeError('NATIVE_ADAPTER_NOT_IMPLEMENTED')
    if not RESULT['checks']:
        return finish('ERROR', 'NO_TESTS_EXECUTED')
    RESULT['required_tests_executed'] = True
    return finish('PASS' if passed else 'FAIL', 'ALL_REQUIRED_CHECKS_PASSED' if passed else 'ASSERTION_FAILED')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        RESULT['evidence']['error'] = traceback.format_exc()
        sys.exit(finish('ERROR', 'EVALUATOR_ERROR'))
