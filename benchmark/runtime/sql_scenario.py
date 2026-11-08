"""Declarative SQL behavior checks. No patch/phase-dependent expected results."""
import json
from pathlib import Path


def matches(actual, expected):
    if not expected:
        return False
    for key, value in expected.items():
        if key == 'success':
            if (actual['rc'] == 0) != value:
                return False
        elif key == 'stdout':
            if actual['stdout'] != value:
                return False
        elif key == 'stderr_contains':
            if value not in actual['stderr']:
                return False
        else:
            raise ValueError('Unknown assertion: ' + key)
    return True


def main():
    from database_support import Postgres, new_observations
    scenario = json.loads(Path('/checks/scenario.json').read_text())
    observations = new_observations(scenario['instance_id'])
    pg = Postgres(observations)
    checks = []
    bugs = []
    try:
        for step in scenario['steps']:
            results = [pg.sql(q['sql'], role=q.get('role', 'bench')) for q in step['queries']]
            healthy = all(matches(a, q['expect']) for a, q in zip(results, step['queries']))
            if step.get('kind') == 'setup':
                if not healthy:
                    raise RuntimeError('SETUP_FAILED: ' + json.dumps(results))
                continue
            kind = step['kind']
            checks.append({'name': step['name'], 'kind': kind, 'passed': healthy,
                           'actual': results, 'expected': [q['expect'] for q in step['queries']]})
            if kind == 'target':
                # Every query must describe the specific original defect, not an arbitrary failure.
                bugs.append(not healthy and all(matches(a, q.get('bug')) for a, q in zip(results, step['queries'])))
        controls_ok = all(c['passed'] for c in checks if c['kind'] == 'control')
        print('CHECK_RESULT=' + json.dumps({'checks': checks, 'reproduced': bool(bugs) and all(bugs) and controls_ok,
              'origin': 'generated_from_problem_statement', 'transcript': observations['transcript']}), flush=True)
    finally:
        pg.stop()


if __name__ == '__main__':
    main()
