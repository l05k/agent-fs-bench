"""One self-contained JSONL record per measurement, without format versions."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import socket
import sys
from typing import Any, TextIO

from .targets import Target

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f'not JSON serializable: {type(value).__name__}')


def canonical_json(value: Any) -> str:
    return json.dumps(value, default=json_default, sort_keys=True,
                      ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def source_digest() -> str:
    """Identify the actual Python sources and fixed inputs, including local edits."""
    digest = hashlib.sha256()
    paths = list((PROJECT_ROOT / 'agent_fs_bench').rglob('*.py'))
    paths += [PROJECT_ROOT / 'pyproject.toml', PROJECT_ROOT / 'uv.lock']
    paths += [path for path in (PROJECT_ROOT / 'data').rglob('*')
              if path.is_file() and 'source-repo' not in path.parts
              and '.git' not in path.parts and '__pycache__' not in path.parts]
    for path in sorted(paths):
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def environment() -> dict[str, Any]:
    return {
        'hostname': socket.gethostname(),
        'platform': platform.platform(),
        'machine': platform.machine(),
        'python': platform.python_version(),
        'source_sha256': source_digest(),
        'argv': sys.argv[1:],
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunContext:
    benchmark: str
    run_id: str
    target: Target
    work_dir: Path
    result_dir: Path
    parameters: dict[str, Any]
    environment: dict[str, Any]
    output: TextIO = field(repr=False)
    records: int = 0

    def emit(self, case: str, metrics: dict[str, int | float], *,
             repeat: int = 1, details: dict[str, Any] | None = None) -> None:
        if not metrics or any(isinstance(value, bool)
                              or not isinstance(value, (int, float))
                              or not math.isfinite(value)
                              for value in metrics.values()):
            raise ValueError('metrics must contain finite numeric measurements')
        parameters = canonical_json(self.parameters)
        row = {
            'benchmark': self.benchmark,
            'run_id': self.run_id,
            'timestamp': utc_now(),
            'target': {'name': self.target.name, 'path': str(self.target.path)},
            'case': case,
            'repeat': repeat,
            'parameters': json.loads(parameters),
            'parameter_id': hashlib.sha256(parameters.encode()).hexdigest(),
            'environment': self.environment,
            'metrics': metrics,
            'details': details or {},
        }
        self.output.write(canonical_json(row) + '\n')
        self.output.flush()
        self.records += 1
