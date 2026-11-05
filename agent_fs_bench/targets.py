"""Explicit directories, not filesystem vendors or mount commands."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class Target:
    name: str
    path: Path


def target_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path('/mnt') / path
    if '..' in path.parts:
        raise ValueError(f'target path must not contain ..: {value}')
    return path


def load_targets(config: Path | None, directories: list[str] | None) -> list[Target]:
    if directories is not None:
        targets = [Target(target_path(value).name, target_path(value))
                   for value in directories]
    else:
        with config.expanduser().open('rb') as handle:
            entries = tomllib.load(handle)['targets']
        targets = [Target(entry['name'], target_path(entry['path']))
                   for entry in entries]
    names: set[str] = set()
    paths: set[Path] = set()
    for target in targets:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', target.name):
            raise ValueError(f'invalid target name: {target.name!r}')
        if target.name in names or target.path in paths:
            raise ValueError(f'duplicate target name or directory: {target.name}')
        if target.path == Path('/'):
            raise ValueError('target must not be the filesystem root')
        names.add(target.name)
        paths.add(target.path)
    if not targets:
        raise ValueError('at least one target directory is required')
    return targets
