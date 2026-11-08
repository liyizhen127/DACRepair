"""Single-file case validation and offline test/problem maintenance.

No command here executes a sample test, database, build, or Docker container.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import uuid

from .util import now, save, sha

DATA_FIELDS = ('repo', 'instance_id', 'base_commit', 'patch', 'test_patch',
               'problem_statement')
PACKAGED_ORIGINS = {'generated', 'upstream_and_generated'}


def record_path(root, instance_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', instance_id) or instance_id in {'.', '..'}:
        raise ValueError('Invalid instance_id')
    return Path(root) / 'confirmed_fixed' / (instance_id + '.json')


def patch_files(patch):
    """Read trusted test-diff file names without interpreting hunk contents."""
    result = set()
    for line in patch.splitlines():
        if not line.startswith('diff --git '):
            continue
        words = shlex.split(line)
        if len(words) != 4:
            raise ValueError('Malformed test patch header')
        for name in words[2:]:
            if not name.startswith(('a/', 'b/')):
                raise ValueError('Expected prefixed patch path')
            name = name[2:]
            if PurePosixPath(name).is_absolute() or any(p in {'', '..', '.', '.git'} for p in name.split('/')) or '\\' in name:
                raise ValueError('Unsafe test patch path')
            result.add(name)
    if patch.strip() and not result:
        raise ValueError('Expected a Git unified test diff')
    return result


def split_test_patch(record):
    """Separate a byte-preserved upstream prefix from this case's added files."""
    prefix = '.acrepair/tests/' + record['instance_id'] + '/'
    native, packaged = [], []
    for block in re.split(r'(?=^diff --git )', record['test_patch'], flags=re.M):
        if not block:
            continue
        if not block.startswith('diff --git '):
            if packaged:
                raise ValueError('Unexpected text after packaged tests')
            native.append(block)
            continue
        paths = patch_files(block)
        local = [p.startswith(prefix) for p in paths]
        if any(local) and not all(local):
            raise ValueError('Test patch crosses package boundary')
        if all(local):
            packaged.append(block)
        elif packaged:
            raise ValueError('Upstream test blocks must precede packaged tests')
        else:
            native.append(block)
    return ''.join(native), ''.join(packaged)


def generated_payload(record):
    """Reconstruct self-contained added test files, preserving their exact bytes."""
    if record['evaluation_spec']['test_origin'] not in PACKAGED_ORIGINS:
        raise ValueError('Upstream patches need their base source tree; use test_patch directly or --test-patch to update')
    prefix = '.acrepair/tests/' + record['instance_id'] + '/'
    native, packaged = split_test_patch(record)
    if record['evaluation_spec']['test_origin'] == 'generated' and native.strip():
        raise ValueError('Generated-only tests contain an upstream patch')
    if record['evaluation_spec']['test_origin'] == 'upstream_and_generated' and not native.strip():
        raise ValueError('Mixed tests require an upstream patch')
    payload = {}
    for block in re.split(r'(?=^diff --git )', packaged, flags=re.M):
        if not block.strip():
            continue
        lines = block.splitlines(keepends=True)
        words = shlex.split(lines[0])
        if len(words) != 4 or words[:2] != ['diff', '--git']:
            raise ValueError('Malformed generated patch')
        target = words[3]
        if not target.startswith('b/' + prefix) or words[2] != 'a/' + target[2:]:
            raise ValueError('Generated file is outside this sample test directory')
        name = target[len('b/' + prefix):]
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or any(p in {'', '.', '..', '.git'} for p in name.split('/')):
            raise ValueError('Unsafe generated file name: ' + name)
        if name in payload or 'new file mode 100644\n' not in lines:
            raise ValueError('Expected unique regular added test file: ' + name)
        hunks = [i for i, line in enumerate(lines) if line.startswith('@@ ')]
        if not hunks:
            payload[name] = b''
            continue
        if len(hunks) != 1 or '--- /dev/null\n' not in lines:
            raise ValueError('Generated test must be a complete new file: ' + name)
        start = hunks[0]
        match = re.fullmatch(r'@@ -0,0 \+1,(\d+) @@\n', lines[start])
        if not match or lines[start - 1] != '+++ ' + target + '\n':
            raise ValueError('Malformed new-file hunk: ' + name)
        body = []
        for line in lines[start + 1:]:
            if line.startswith('+'):
                body.append(line[1:])
            elif line == '\\ No newline at end of file\n' and body and body[-1].endswith('\n'):
                body[-1] = body[-1][:-1]
            else:
                raise ValueError('Unexpected line in generated hunk: ' + name)
        if len(body) != int(match[1]):
            raise ValueError('Generated hunk line count mismatch: ' + name)
        payload[name] = ''.join(body).encode('utf-8')
    return payload


def generated_patch(iid, payload):
    blocks = []
    for name, data in sorted(payload.items()):
        path = '.acrepair/tests/' + iid + '/' + name
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path.split('/'):
            raise ValueError('Unsafe test filename')
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        header = f'diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..{blob}\n'
        if data:
            lines = data.decode('utf-8').split('\n')
            ends_newline = lines[-1] == ''
            if ends_newline:
                lines.pop()
            header += f'--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n'
            header += ''.join('+' + line + '\n' for line in lines)
            if not ends_newline:
                header += '\\ No newline at end of file\n'
        blocks.append(header)
    return ''.join(blocks)


def refresh_entry(record, payload):
    """Refresh metadata assignments, preserving the actual authored entry logic."""
    text = payload['test.py'].decode('utf-8')
    values = {'INSTANCE_ID': record['instance_id'],
              'REQUIRED_CHECK_IDS': record['evaluation_spec']['required_check_ids'],
              'TEST_STATUS': record['evaluation_spec']['test_status'],
              'EXPECTED_FILES': {name: sha(data) for name, data in sorted(payload.items()) if name != 'test.py'}}
    lines = text.splitlines(keepends=True)
    edits = []
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in values:
                edits.append((node.lineno - 1, node.end_lineno, name + ' = ' + repr(values.pop(name)) + '\n'))
    if values:
        raise ValueError('Generated test.py lacks required metadata assignments: ' + ', '.join(values))
    for first, last, replacement in reversed(edits):
        lines[first:last] = [replacement]
    payload['test.py'] = ''.join(lines).encode('utf-8')


def validate_record(record):
    """Static schema/material checks, never a claim of runtime correctness."""
    for name in DATA_FIELDS:
        if not isinstance(record.get(name), str):
            raise ValueError('Missing or invalid record field: ' + name)
    record_path('.', record['instance_id'])
    for name in ('environment_spec', 'evaluation_spec'):
        if not isinstance(record.get(name), dict):
            raise ValueError('Missing object: ' + name)
    for key in ('assets', 'bundle_root', 'packaged_files', 'reference_patch', 'effective_test_patch'):
        if key in record['evaluation_spec']:
            raise ValueError('External material dependency is not supported: ' + key)
    if 'record_sha256' in record or 'record' in record:
        raise ValueError('No self-referential record path/hash in a single-file sample')
    if any(key in record for key in ('FAIL_TO_PASS', 'PASS_TO_PASS')):
        raise ValueError('F2P/P2P fields are not part of this dataset')
    spec = record['evaluation_spec']
    env = record['environment_spec']
    for name in ('engine', 'adapter_status', 'platform', 'toolchain'):
        if not isinstance(env.get(name), str) or not env[name]:
            raise ValueError('Missing environment field: ' + name)
    if env['adapter_status'] not in {'implemented', 'pending'} or env['platform'] != 'linux/amd64':
        raise ValueError('Unsupported environment status/platform')
    if {'jobs', 'cpus', 'memory'} & env.keys():
        raise ValueError('jobs, cpus and memory are runtime options; pass them to Benchmark or the CLI')
    if spec.get('mode') not in {'missing', 'pending_adapter', 'native', 'focused_and_native', 'generated', 'blocked'}:
        raise ValueError('Unsupported evaluation mode')
    if spec.get('test_origin') not in {'missing', 'upstream', 'upstream_recovered'} | PACKAGED_ORIGINS:
        raise ValueError('Unsupported test origin')
    if spec.get('focused') not in {None, 'generated'}:
        raise ValueError('Focused tests must use the sample test_patch entrypoint')
    for name in ('required_check_ids', 'native_tests', 'protected_test_paths'):
        if not isinstance(spec.get(name), list) or any(not isinstance(s, str) for s in spec[name]):
            raise ValueError('Invalid evaluation field: ' + name)
    if not isinstance(record.get('blockers'), list) or any(not isinstance(s, str) for s in record['blockers']):
        raise ValueError('Invalid blockers')
    unlisted = patch_files(record['test_patch']) - set(spec['protected_test_paths'])
    if any(not set(path.split('/')) & {'test', 'tests', 'mysql-test', 'regression-test'} for path in unlisted):
        raise ValueError('Test paths missing from protection list; use update-case')
    if spec['test_origin'] not in PACKAGED_ORIGINS:
        return
    payload = generated_payload(record)
    if not {'test.py', 'case_body.py', 'scenario.json', 'record.json'} <= payload.keys():
        raise ValueError('Incomplete generated test payload')
    expected_entry = '.acrepair/tests/' + record['instance_id'] + '/test.py'
    if spec.get('test_entrypoint') != expected_entry:
        raise ValueError('Generated test entrypoint does not match sample identity')
    if spec.get('focused') != 'generated':
        raise ValueError('Packaged tests require the focused entrypoint')
    identity = json.loads(payload['record.json'])
    if identity != {'instance_id': record['instance_id'], 'repo': record['repo']}:
        raise ValueError('Generated test identity mismatch')
    scenario = json.loads(payload['scenario.json'])
    if scenario.get('instance_id') != record['instance_id']:
        raise ValueError('Generated scenario identity mismatch')
    constants = {}
    for name, data in payload.items():
        if name.endswith('.py'):
            tree = ast.parse(data.decode('utf-8'), filename=name)
            if name == 'test.py':
                for node in tree.body:
                    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                        if node.targets[0].id in {'INSTANCE_ID', 'REQUIRED_CHECK_IDS', 'TEST_STATUS', 'EXPECTED_FILES'}:
                            constants[node.targets[0].id] = ast.literal_eval(node.value)
    if constants.get('EXPECTED_FILES') != {n: sha(d) for n, d in payload.items() if n != 'test.py'}:
        raise ValueError('Generated test hashes are stale; use update-case --test-dir')
    if constants.get('INSTANCE_ID') != record['instance_id'] or constants.get('TEST_STATUS') != spec['test_status']:
        raise ValueError('Generated entry metadata mismatch')
    if not isinstance(constants.get('REQUIRED_CHECK_IDS'), list) or not constants['REQUIRED_CHECK_IDS']:
        raise ValueError('Generated entry has no required checks')
    if constants['REQUIRED_CHECK_IDS'] != spec['required_check_ids']:
        raise ValueError('Generated entry required checks differ from evaluation spec')


def unpack_tests(record, output):
    payload = generated_payload(record)
    folder = Path(output).resolve()
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError('Output must be a new or empty directory')
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in payload.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {'instance_id': record['instance_id'], 'files': len(payload), 'output': str(folder), 'tests_executed': 0}


def update_case(root, iid, *, test_dir=None, test_patch=None, problem=None,
                config=None, source_urls=()):
    """Atomically update one case; retain a durable before/after change receipt."""
    import fcntl
    path = record_path(root, iid)
    changes = Path(root).resolve().parent / 'OldData' / 'case_updates'
    changes.mkdir(parents=True, exist_ok=True)
    with (changes / (iid + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.read_bytes()
        original = json.loads(before)
        record = copy.deepcopy(original)
        if config:
            overrides = json.loads(Path(config).read_text())
            if not isinstance(overrides, dict) or set(overrides) - {'environment_spec', 'evaluation_spec', 'blockers'}:
                raise ValueError('Config accepts only environment_spec, evaluation_spec and blockers')
            record.update(overrides)
        if problem:
            record['problem_statement'] = Path(problem).read_bytes().decode('utf-8')
        if test_dir and test_patch:
            raise ValueError('Choose --test-dir or --test-patch, not both')
        if test_dir:
            if record['evaluation_spec']['test_origin'] not in PACKAGED_ORIGINS:
                raise ValueError('--test-dir is for generated tests; upstream tests use --test-patch')
            folder = Path(test_dir).resolve()
            payload = {}
            for item in sorted(folder.rglob('*')):
                if item.is_symlink():
                    raise ValueError('Test inputs may not contain symlinks')
                if item.is_file() and '__pycache__' not in item.parts and item.name != '.DS_Store':
                    payload[item.relative_to(folder).as_posix()] = item.read_bytes()
            refresh_entry(record, payload)
            native, _ = split_test_patch(record)
            record['test_patch'] = native + generated_patch(iid, payload)
        elif test_patch:
            record['test_patch'] = Path(test_patch).read_bytes().decode('utf-8')
        spec = record['evaluation_spec']
        spec['protected_test_paths'] = sorted(set(spec['protected_test_paths']) | patch_files(record['test_patch']))
        for url in source_urls:
            if not url.startswith(('https://', 'http://')):
                raise ValueError('Source URL must use HTTP(S)')
        validate_record(record)
        record_changed = record != original
        if not record_changed and not source_urls:
            return {'instance_id': iid, 'changed': False, 'tests_executed': 0}
        folder = changes / (iid + '-' + uuid.uuid4().hex[:12])
        folder.mkdir()
        (folder / 'before.json').write_bytes(before)
        save(folder / 'after.json', record)
        receipt = {'instance_id': iid, 'time': now(), 'status': 'PREPARED',
                   'before_sha256': sha(before), 'after_sha256': sha((folder / 'after.json').read_bytes()),
                   'record_changed': record_changed,
                   'tests_executed': 0, 'runtime_validation_required': record_changed}
        if source_urls:
            receipt['source_urls'] = sorted(set(source_urls))
        save(folder / 'receipt.json', receipt)
        if path.read_bytes() != before:
            raise ValueError('Concurrent sample modification detected; no canonical write made')
        if record_changed:
            save(path, record)
        save(folder / 'receipt.json', {**receipt, 'status': 'APPLIED'})
        return {'instance_id': iid, 'changed': record_changed, 'receipt': str(folder / 'receipt.json'),
                'tests_executed': 0, 'runtime_validation_required': record_changed}
