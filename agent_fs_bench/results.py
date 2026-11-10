"""Per-run results: config.json (parameter snapshot) plus one flat CSV per observation kind.

Every CSV row is one observation: key columns (experimental conditions) first, then
metric columns with their unit in the name. Each table has one granularity; a metric
that does not apply to an observation belongs in another table, never in an empty cell.
"""

import ast
import csv
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any

from .targets import Target

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def source_digest() -> str:
    """Identify the Python sources and fixed inputs, including local edits.

    Each module's top-level CONFIG literal is excluded: config.json records a
    workload's values as `config` (the launcher CONFIG of `cluster.py` as
    `config.launcher`), so one sweep can edit CONFIG per run (e.g. `tasks`)
    while any other source change still yields a new fingerprint.
    """
    digest = hashlib.sha256()
    paths = list((PROJECT_ROOT / 'agent_fs_bench').rglob('*.py'))
    paths += [PROJECT_ROOT / 'pyproject.toml', PROJECT_ROOT / 'uv.lock']
    paths += [path for path in (PROJECT_ROOT / 'data').rglob('*')
              if path.is_file() and 'source-repo' not in path.parts
              and '.git' not in path.parts and '__pycache__' not in path.parts]
    for path in sorted(paths):
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode())
        digest.update(b'\0')
        digest.update(_without_config(path) if path.suffix == '.py' else path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def _without_config(path: Path) -> bytes:
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines(keepends=True)
    for node in reversed(ast.parse(text).body):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(target, ast.Name) and target.id == 'CONFIG' for target in targets):
            del lines[node.lineno - 1:node.end_lineno]
    return ''.join(lines).encode()


def git_commit() -> str:
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=PROJECT_ROOT, capture_output=True,
                          text=True, check=True).stdout.strip()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n',
                    encoding='utf-8')


def read_table(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


class Tables:
    """Append-only `<table>.csv` files; a table's first row fixes its columns."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.schemas: dict[str, dict[str, list[str]]] = {}
        self._files: dict[str, tuple[Any, csv.DictWriter]] = {}

    def write(self, table: str, keys: dict[str, Any], metrics: dict[str, Any]) -> None:
        schema = {'keys': list(keys), 'metrics': list(metrics)}
        if table not in self.schemas:
            handle = (self.directory / f'{table}.csv').open('x', newline='', encoding='utf-8')
            writer = csv.DictWriter(handle, fieldnames=schema['keys'] + schema['metrics'])
            writer.writeheader()
            self.schemas[table] = schema
            self._files[table] = (handle, writer)
        assert self.schemas[table] == schema, \
            f'{table} columns: expected {self.schemas[table]}, measured {schema}'
        handle, writer = self._files[table]
        writer.writerow({name: int(value) if isinstance(value, bool) else value
                         for name, value in {**keys, **metrics}.items()})
        handle.flush()

    def close(self) -> None:
        for handle, _ in self._files.values():
            handle.close()


@dataclass
class RunContext:
    benchmark: str
    run_id: str
    target: Target
    work_dir: Path
    result_dir: Path
    tables: Tables
    info: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    pool: Any = None  # cluster.Pool of the machines this run may use

    def emit(self, table: str, keys: dict[str, Any], metrics: dict[str, int | float]) -> None:
        """Append one observation; `target` is prepended to the key columns."""
        assert metrics, f'{table} metrics: expected at least one, measured none'
        for name, value in metrics.items():
            assert (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)), \
                f'{table}.{name}: expected a finite number, measured {value!r}'
        self.tables.write(table, {'target': self.target.name, **keys}, metrics)
        self.rows += 1
