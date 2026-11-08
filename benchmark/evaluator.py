"""Host orchestrator. Never treats a successful batch process as a successful repair."""
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import uuid
from .models import EvaluationResult
from .docker import build_image, docker
from .util import now, sha, save


def patch_paths(patch):
    if not patch.strip():
        return set()
    if 'GIT binary patch' in patch or re.search(r'^Binary files ', patch, re.M):
        raise ValueError('Binary patches are not accepted by this benchmark')
    paths = set()
    def add(name, prefixed=True):
        if name == '/dev/null':
            return
        if prefixed:
            if not name.startswith(('a/', 'b/')):
                raise ValueError('Patch requires a/ and b/ paths')
            name = name[2:]
        if (not name or '\\' in name or PurePosixPath(name).is_absolute()
                or any(p in ('', '.', '..', '.git') for p in name.split('/'))):
            raise ValueError('Unsafe patch path')
        paths.add(name)
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith('diff --git '):
            in_hunk = False
            pieces = shlex.split(line)
            if len(pieces) != 4:
                raise ValueError('Malformed diff header')
            for name in pieces[2:]:
                add(name)
        elif line.startswith('@@ '):
            in_hunk = True
        elif not in_hunk and line.startswith(('--- ', '+++ ')):
            pieces = shlex.split(line[4:].split('\t')[0])
            if len(pieces) != 1:
                raise ValueError('Malformed file header')
            add(pieces[0])
        elif not in_hunk and line.startswith(('rename from ', 'rename to ', 'copy from ', 'copy to ')):
            raise ValueError('Rename/copy metadata is not supported; submit ordinary file diffs')
        if re.match(r'^(?:new file mode|new mode) (?:120000|160000)', line):
            raise ValueError('New symlinks and submodules are not accepted')
    if not paths:
        raise ValueError('Expected a unified Git diff')
    return paths


def validate_candidate(patch, spec):
    paths = patch_paths(patch)
    protected = set(spec.get('protected_test_paths', []))
    for path in paths:
        if path in protected or any(p in {'test', 'tests', 'mysql-test', 'regression-test'} for p in path.split('/')):
            raise ValueError(f'Candidate modifies evaluator-owned tests: {path}')
    return paths


def emit(root, event, **details):
    path = root / 'runs' / 'events.jsonl'
    path.parent.mkdir(exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps({'time': now(), 'event': event, **details}, ensure_ascii=False) + '\n')
        stream.flush()


def evaluate(case, repair_patch, *, require_verified=False, reference_mode=False):
    if not isinstance(repair_patch, str):
        raise TypeError('repair_patch must be unified diff text, not a filename')
    resources = case.resources.to_dict()
    blockers = case.plan_blockers
    if require_verified and not case.readiness['runtime_verified']:
        blockers += case.readiness['verification_notes']
    if blockers:
        return EvaluationResult(case.instance_id, 'ERROR', 'CASE_NOT_READY',
                                evidence={'blockers': blockers, 'runtime_resources': resources})
    try:
        # Reference source patches can contain manifest/test-support maintenance. The trusted test_patch is separate.
        patch_paths(repair_patch) if reference_mode else validate_candidate(repair_patch, case.manifest['evaluation_spec'])
    except ValueError as exc:
        return EvaluationResult(case.instance_id, 'FAIL', 'INVALID_REPAIR_PATCH',
                                evidence={'error': str(exc), 'runtime_resources': resources})
    run_id = case.instance_id + '-' + uuid.uuid4().hex[:12]
    folder = case.root / 'runs' / run_id
    bundle = folder / 'bundle'
    bundle.mkdir(parents=True)
    log_dir = str(folder.relative_to(case.root))
    state = {'instance_id': case.instance_id, 'run_id': run_id, 'started_at': now(),
             'runtime_resources': resources,
             'fingerprint': case.fingerprint, 'patch_sha256': sha(repair_patch.encode()), 'status': 'RUNNING'}
    save(folder / 'progress.json', state)
    emit(case.root, 'EVALUATION_START', run_id=run_id, instance_id=case.instance_id,
         runtime_resources=resources)
    cname = 'acrepair-eval-' + uuid.uuid4().hex[:16]
    try:
        image = build_image(case)
        if require_verified:
            expected = case.readiness['paired_validation']['reference']['evidence']['image_id']
            if image['image_id'] != expected:
                raise RuntimeError('Environment image changed; paired validation is required on this host.')
        for p in (case.root / 'benchmark' / 'runtime').iterdir():
            if p.is_file():
                shutil.copy2(p, bundle / p.name)
        record = case._record
        # test_patch contains all evaluator-owned assets, including generated
        # helpers; runtime/entry.py applies it under /src before running tests.
        (bundle / 'candidate.patch').write_bytes(repair_patch.encode('utf-8'))
        (bundle / 'test_patch.diff').write_bytes(record['test_patch'].encode('utf-8'))
        save(bundle / 'record.json', {'instance_id': case.instance_id, 'repo': record['repo']})
        save(bundle / 'plan.json', {'instance_id': case.instance_id,
             'engine': case.manifest['environment_spec']['engine'], 'evaluation': case.manifest['evaluation_spec']})
        save(folder / 'environment.json', {**image, 'runtime_resources': resources})
        spec = case.manifest['environment_spec']
        argv = ['docker', 'run', '--name', cname, '--platform', spec['platform'], '--network=none',
                *case.resources.docker_run_args(), '--shm-size=1g',
                '--mount', f'type=bind,src={bundle},dst=/checks,readonly', image['image_id'],
                'python3', '/checks/entry.py']
        with (folder / 'container.log').open('w') as stream:
            stream.write(json.dumps({'command': argv}) + '\n')
            stream.flush()
            try:
                p = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=9000)
                exit_code = p.returncode
            except subprocess.TimeoutExpired:
                docker(['stop', '-t', '2', cname], check=False)
                exit_code = 124
        copied = docker(['cp', cname + ':/tmp/acrepair-evaluation', str(folder / 'output')], check=False)
        rawpath = folder / 'output' / 'result.json'
        if copied.returncode or not rawpath.exists():
            result = EvaluationResult(case.instance_id, 'ERROR', 'EVALUATOR_DID_NOT_FINISH', log_dir=log_dir,
                                      evidence={'container_exit_code': exit_code})
        else:
            raw = json.loads(rawpath.read_text())
            result = EvaluationResult.from_dict(raw)
            result.log_dir = log_dir
            if exit_code != result.exit_code:
                result.status, result.reason = 'ERROR', 'EXIT_CODE_RESULT_MISMATCH'
            if result.status == 'PASS' and (not result.patch_applied or not result.build_succeeded or
                    not result.required_tests_executed or not result.checks or
                    any(c.get('passed') is not True for c in result.checks)):
                result.status, result.reason = 'ERROR', 'INVALID_PASS_RESULT'
        result.evidence.update({'image_id': image['image_id'], 'environment_key': image['environment_key'],
                                'source': image['source'], 'candidate_patch_sha256': sha(repair_patch.encode()),
                                'fingerprint': case.fingerprint})
    except Exception as exc:
        result = EvaluationResult(case.instance_id, 'ERROR', 'INFRASTRUCTURE_ERROR', log_dir=log_dir,
                                  evidence={'error': repr(exc)})
    finally:
        try:
            docker(['rm', '-f', cname], check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    result.evidence['runtime_resources'] = resources
    save(folder / 'result.json', result.to_dict())
    state.update(status=result.status, finished_at=now(), reason=result.reason)
    save(folder / 'progress.json', state)
    emit(case.root, 'EVALUATION_END', run_id=run_id, instance_id=case.instance_id,
         status=result.status, reason=result.reason)
    return result


def validate_reference(case):
    import fcntl
    lock = case.root / 'benchmark' / 'state' / (case.instance_id + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        return _validate_reference(case)


def _validate_reference(case):
    path = case.root / 'benchmark' / 'state' / (case.instance_id + '.json')
    fingerprint = case.fingerprint
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get('fingerprint') == fingerprint and state.get('status') == 'VALIDATED':
        try:
            current_image = build_image(case)
        except Exception:
            # Leave the completed proof intact; the caller can retry after infrastructure recovers.
            raise
        if current_image['image_id'] == state['reference']['evidence']['image_id']:
            return state
        save(case.root / 'runs' / 'validation_history' / (case.instance_id + '-' + uuid.uuid4().hex[:8] + '.json'), state)
        state = {}
    if state.get('fingerprint') != fingerprint:
        if state:
            save(case.root / 'runs' / 'validation_history' / (case.instance_id + '-' + uuid.uuid4().hex[:8] + '.json'), state)
        state = {'instance_id': case.instance_id, 'fingerprint': fingerprint,
                 'runtime_resources': case.resources.to_dict(), 'started_at': now(), 'status': 'INCOMPLETE'}
    save(path, state)
    record = case._record
    reference = record['patch']
    for phase, patch in [('baseline', ''), ('reference', reference)]:
        if phase in state and state[phase].get('status') != 'ERROR':
            continue
        result = evaluate(case, patch, reference_mode=True)
        state[phase] = result.to_dict()
        state['updated_at'] = now()
        save(path, state)
    baseline, reference = state['baseline'], state['reference']
    if baseline['status'] == 'ERROR' or reference['status'] == 'ERROR':
        state['status'] = 'EVALUATION_ERROR'
    elif not baseline.get('build_succeeded') or not baseline.get('required_tests_executed'):
        state['status'] = 'BASELINE_NOT_TESTABLE'
    elif baseline['status'] != 'FAIL' or not baseline.get('evidence', {}).get('observed_bug'):
        state['status'] = 'NOT_REPRODUCED'
    elif reference['status'] != 'PASS':
        state['status'] = 'REFERENCE_PATCH_FAILED'
    elif baseline['evidence'].get('image_id') != reference['evidence'].get('image_id'):
        state['status'] = 'ENVIRONMENT_CHANGED_BETWEEN_PHASES'
    else:
        state['status'] = 'VALIDATED'
    state['finished_at'] = now()
    save(path, state)
    return state
