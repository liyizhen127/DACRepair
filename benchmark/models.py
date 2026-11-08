from dataclasses import dataclass, field, asdict
import math
import re


class NotReadyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeResources:
    """Host-supplied execution settings, never taken from a sample record."""
    jobs: int = 12
    cpus: float = 12
    memory: str = '28g'

    def __post_init__(self):
        if type(self.jobs) is not int or self.jobs <= 0:
            raise ValueError('jobs must be a positive integer')
        if (type(self.cpus) not in (int, float) or not math.isfinite(self.cpus)
                or self.cpus <= 0):
            raise ValueError('cpus must be a positive finite number')
        if not isinstance(self.memory, str):
            raise ValueError('memory must be a positive Docker size, such as 28g or 512m')
        memory = self.memory.strip().lower()
        match = re.fullmatch(r'([0-9]+)([bkmg]?)', memory)
        if not match or int(match[1]) <= 0:
            raise ValueError('memory must be a positive Docker size, such as 28g or 512m')
        object.__setattr__(self, 'cpus', float(self.cpus))
        object.__setattr__(self, 'memory', str(int(match[1])) + match[2])

    def to_dict(self):
        return asdict(self)

    def docker_run_args(self):
        return ['--memory=' + self.memory, '--cpus=' + format(self.cpus, 'g'),
                '--env', 'MAKE_JOBS=' + str(self.jobs)]


@dataclass
class EvaluationResult:
    instance_id: str
    status: str  # PASS / FAIL / ERROR
    reason: str
    patch_applied: bool = False
    build_succeeded: bool = False
    required_tests_executed: bool = False
    checks: list = field(default_factory=list)
    log_dir: str | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def exit_code(self):
        return {'PASS': 0, 'FAIL': 1, 'ERROR': 2}[self.status]

    def to_dict(self):
        return {**asdict(self), 'exit_code': self.exit_code}

    @classmethod
    def from_dict(cls, value):
        return cls(**{k: value[k] for k in cls.__dataclass_fields__ if k in value})
