"""Public two-step API; collection records are never handed to an agent."""
import json
from pathlib import Path
from .models import NotReadyError, RuntimeResources
from .util import sha
from .case_tools import record_path, validate_record


class Benchmark:
    def __init__(self, root=None, *, jobs=12, cpus=12, memory='28g'):
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        self.resources = RuntimeResources(jobs=jobs, cpus=cpus, memory=memory)

    def load(self, instance_id):
        path = record_path(self.root, instance_id)
        if not path.is_file():
            raise KeyError(instance_id)
        record = json.loads(path.read_bytes())
        if record['instance_id'] != instance_id:
            raise ValueError('Sample identity does not match its filename')
        validate_record(record)
        return BenchmarkCase(self.root, record, resources=self.resources)

    def cases(self, ready_only=False):
        for path in sorted((self.root / 'confirmed_fixed').glob('*.json')):
            case = self.load(path.stem)
            if not ready_only or case.readiness['ready']:
                yield case


class BenchmarkCase:
    def __init__(self, root, manifest, *, resources=None):
        self.root = Path(root)
        self.manifest = manifest
        self.resources = resources if resources is not None else RuntimeResources()
        self.instance_id = manifest['instance_id']
        self._record_path = record_path(root, self.instance_id)
        self._loaded_hash = sha(self._record_path.read_bytes())

    @property
    def _record(self):
        raw = self._record_path.read_bytes()
        if sha(raw) != self._loaded_hash:
            raise ValueError('The sample changed after loading; reload it before repair/evaluation.')
        return json.loads(raw)

    @property
    def problem_statement(self):
        return self._record['problem_statement']

    @property
    def fingerprint(self):
        # Changes to source record, tests, setup, or scoring invalidate paired proof.
        data = json.dumps(self.manifest, sort_keys=True).encode()
        data += self._record_path.read_bytes()
        data += json.dumps(self.resources.to_dict(), sort_keys=True).encode()
        package = self.root / 'benchmark'
        files = [package / n for n in ('core.py', 'docker.py', 'evaluator.py', 'models.py', 'util.py', 'case_tools.py')]
        files += [package / 'environments' / 'Dockerfile.toolchain']
        files += sorted(p for p in (package / 'runtime').rglob('*') if p.is_file() and '__pycache__' not in p.parts)
        for path in files:
            data += str(path.relative_to(self.root)).encode() + b'\0' + path.read_bytes()
        return sha(data)

    @property
    def plan_blockers(self):
        reasons = list(self.manifest.get('blockers', []))
        if self.manifest['environment_spec'].get('adapter_status') != 'implemented':
            reasons.append('ENVIRONMENT_ADAPTER_REQUIRED')
        if self.manifest['evaluation_spec'].get('mode') == 'missing':
            reasons.append('BEHAVIORAL_TEST_REQUIRED')
        if self.manifest['evaluation_spec'].get('test_status') == 'dependency_placeholder':
            reasons.append('TEST_AUTHORING_BLOCKED')
        return sorted(set(reasons))

    @property
    def readiness(self):
        state_path = self.root / 'benchmark' / 'state' / (self.instance_id + '.json')
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        verification = []
        if not state:
            verification.append('NOT_RUNTIME_VERIFIED')
        elif state.get('fingerprint') != self.fingerprint:
            verification.append('VALIDATION_STALE')
        elif state.get('status') != 'VALIDATED':
            verification.append(state.get('status', 'VALIDATION_FAILED'))
        spec = self.manifest['evaluation_spec']
        material_blockers = ([] if spec['test_origin'] != 'missing' and self._record['test_patch'].strip()
                             else ['BEHAVIORAL_TEST_REQUIRED'])
        material_ready = not material_blockers
        return {'ready': material_ready, 'readiness_basis': 'test_materials_available_no_runtime_gate',
                'blockers': material_blockers,
                'execution_ready': not self.plan_blockers, 'execution_blockers': self.plan_blockers,
                'runtime_verified': not verification, 'verification_notes': verification,
                'paired_validation': state}

    def create_repair_env(self, *, platform='linux/amd64', require_verified=False):
        from .docker import build_image, DockerEnvironment
        if platform != self.manifest['environment_spec']['platform']:
            raise NotReadyError('This environment is pinned to linux/amd64; ARM hosts use Docker emulation.')
        reasons = self.plan_blockers
        if require_verified and not self.readiness['runtime_verified']:
            reasons += self.readiness['verification_notes']
        if reasons:
            raise NotReadyError(', '.join(reasons))
        image = build_image(self)
        if require_verified:
            expected = self.readiness['paired_validation']['reference']['evidence']['image_id']
            if image['image_id'] != expected:
                raise NotReadyError('Environment image changed; run paired validation on this host again.')
        return DockerEnvironment(image, self.manifest['environment_spec'], self.instance_id,
                                 resources=self.resources)

    def evaluate(self, repair_patch, *, require_verified=False):
        from .evaluator import evaluate
        return evaluate(self, repair_patch, require_verified=require_verified)

    def validate_reference(self):
        from .evaluator import validate_reference
        return validate_reference(self)
