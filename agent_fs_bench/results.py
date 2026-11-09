"""Per-run results: manifest.json plus one flat CSV per record kind."""

import csv
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .targets import Target

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
        elif self.schemas[table] != schema:
            raise ValueError(f'{table}: columns {schema} differ from {self.schemas[table]}')
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

    def emit(self, table: str, keys: dict[str, Any], metrics: dict[str, int | float | None]) -> None:
        """Append one row; `target` is prepended to keys, None metrics stay empty."""
        if not any(value is not None for value in metrics.values()) or any(
                value is not None and (isinstance(value, bool)
                                       or not isinstance(value, (int, float))
                                       or not math.isfinite(value))
                for value in metrics.values()):
            raise ValueError(f'{table}: metrics must be finite numbers or None: {metrics}')
        self.tables.write(table, {'target': self.target.name, **keys}, metrics)
        self.rows += 1
