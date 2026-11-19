#!/usr/bin/env python3
"""Time one offline npm pack over installed packages without writing to the workspace."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

PACKAGE = re.compile(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*')


def package_paths(workspace: Path, names: list[str]) -> list[Path]:
    modules = workspace / 'node_modules'
    if not modules.is_dir() or modules.is_symlink() or len(names) != len(set(names)):
        raise ValueError('expected a real node_modules directory and distinct package names')
    paths = []
    for name in names:
        if not PACKAGE.fullmatch(name):
            raise ValueError(f'invalid installed package name: {name!r}')
        path = modules / name
        if path.resolve(strict=True) != path or not path.is_dir():
            raise ValueError(f'expected an installed directory without symlinks: {name}')
        paths.append(path)
    return paths


def npm_environment(root: Path) -> dict[str, str]:
    # Run outside the project and keep npm state and archive writes off the target.
    env = {key: value for key, value in os.environ.items()
           if not key.lower().startswith('npm_config_')}
    env.update(HOME=str(root), TMPDIR=str(root), XDG_CACHE_HOME=str(root),
               XDG_CONFIG_HOME=str(root), XDG_DATA_HOME=str(root), CI='1',
               npm_config_cache=str(root / 'cache'),
               npm_config_userconfig=str(root / 'user.npmrc'),
               npm_config_globalconfig=str(root / 'global.npmrc'),
               npm_config_update_notifier='false')
    return env


def packed_artifacts(stdout: str, names: list[str], archives: Path) -> tuple[int, str]:
    rows = json.loads(stdout)
    if sorted(row['name'] for row in rows) != sorted(names):
        raise ValueError('npm pack did not produce exactly the selected packages')
    digest = hashlib.sha256()
    total = 0
    seen = set()
    for row in sorted(rows, key=lambda row: row['name']):
        filename = row['filename']
        if Path(filename).name != filename or not filename.endswith('.tgz') or filename in seen:
            raise ValueError(f'invalid or duplicate archive name: {filename!r}')
        seen.add(filename)
        path = archives / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'missing regular archive: {filename}')
        with path.open('rb') as stream:
            packed = hashlib.file_digest(stream, 'sha256').digest()
            size = os.fstat(stream.fileno()).st_size
        if size <= 0 or size != row['size']:
            raise ValueError(f'archive size differs from npm result: {filename}')
        total += size
        digest.update(row['name'].encode() + b'\0' + packed)
    return total, digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--packages', nargs='+', required=True)
    args = parser.parse_args()
    workspace, scratch = (path.resolve(strict=True) for path in (args.workspace, args.scratch))
    if not workspace.is_dir() or not scratch.is_dir() or (
            workspace.is_relative_to(scratch) or scratch.is_relative_to(workspace)):
        raise ValueError('workspace and scratch must be separate directories')
    if workspace.stat().st_dev == scratch.stat().st_dev:
        raise ValueError('scratch must be on a different filesystem from the workspace')
    paths = package_paths(workspace, args.packages)
    with TemporaryDirectory(prefix='npm-pack-', dir=scratch) as tmp:
        root = Path(tmp)
        archives = root / 'archives'
        archives.mkdir()
        env = npm_environment(root)
        version = subprocess.check_output(['npm', '--version'], cwd=root, env=env, text=True).strip()
        started = perf_counter()
        result = subprocess.run(['npm', 'pack', '--offline', '--ignore-scripts', '--json',
                                 '--pack-destination', str(archives), *map(str, paths)],
                                cwd=root, env=env, text=True, stdout=subprocess.PIPE, check=True)
        elapsed = perf_counter() - started
        total, digest = packed_artifacts(result.stdout, args.packages, archives)
    print(json.dumps({'elapsed_s': elapsed, 'npm_version': version, 'package_count': len(paths),
                      'packed_bytes': total, 'tarball_digest_sha256': digest}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'npm workspace pack: {error}') from None
