"""Measure mount scaling with the original IOR 4.0.0 mdtest binary.

Each measurement is one mdtest MPI job launched in MPMD form, one rank per
group, so all ranks share ``MPI_COMM_WORLD`` and mdtest's own phase barriers
produce one aggregate ``all`` row::

    <launcher> -n 1 <mdtest> -d <path-0> <options> :
               -n 1 <mdtest> -d <path-1> <options> : ...

Each rank's argv holds one ``-d`` path, so mdtest's ``path_count`` stays one per
rank: several paths are rejected both with ``-S`` (shared file access) and
without ``-u``. Every rank therefore accesses the same rank-independent
``mdtest.shared.<n>`` names through its own client alias, while rank 0 writes
the one CSV for the job. Private phases precreate each workspace's
``test-dir.0-0``, which mdtest creates only for ranks below ``path_count``;
each rank then creates ``mdtest_tree.<rank>.0``. Shared fixtures are created
once by ``prepare`` and never by a measured job.

Timing comes only from mdtest's rank CSV: the ``all`` row is the aggregate
(maximum rank time including the phase-end barrier), each ``<rank>`` row is that
rank's time before the barrier. Command wall time is never a metric and rates
are never summed in Python. The launcher must support MPMD ``:`` syntax.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_VERSION = '4.0.0'
# mdtest's mdtest_test_num_t order; the rank CSV header repeats every name.
_PHASES = (
    'Directory creation', 'Directory stat', 'Directory read', 'Directory rename',
    'Directory removal', 'File creation', 'File stat', 'File read',
    'File removal', 'Tree creation', 'Tree removal',
)
_FILE_PHASES = ('File creation', 'File stat', 'File read', 'File removal')
_HEADER = ['rank', 'items'] + [f'{kind}-{name}' for name in _PHASES
                               for kind in ('rate', 'time')]
# A phase's rate starts at 2 + 2 * index, its time at 3 + 2 * index. Rank rows
# use the same layout but stop before the tree phases and omit the items column.
_TIME = {name: 3 + 2 * index for index, name in enumerate(_PHASES)}
_RANK_ROW = 2 + 2 * _PHASES.index('Tree creation')
_BANNER = re.compile(
    r'^mdtest-(\S+) was launched with (\d+) total task\(s\) on (\d+) node\(s\)\r?$',
    re.MULTILINE,
)
# mdtest's TEST_DIR plus prep_testdir's ".<iteration>-<dir_iter>" for -i 1.
_TEST_DIR = 'test-dir.0-0'


@dataclass(frozen=True)
class Phase:
    name: str
    operations: int
    elapsed_s: float
    rank_elapsed_s: list[float]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _executable(binary: str) -> Path:
    if not binary:
        raise ValueError('mdtest binary must not be empty')
    found = shutil.which(binary) if os.path.dirname(binary) == '' else binary
    if found is None:
        raise ValueError(f'mdtest executable not found on PATH: {binary}')
    path = Path(found).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f'mdtest is not an executable file: {path}')
    return path


def _options(files: int, size: int) -> tuple[str, ...]:
    for name, value, minimum in (('files', files, 1), ('size', size, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
    return ('-F', '-n', str(files), '-w', str(size), '-e', str(size), '-i', '1',
            '--warningAsErrors')


def _tail(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else ''


class Mdtest:
    """Run upstream mdtest 4.0.0 file phases and report their upstream timings."""

    def __init__(self, binary: str, launcher: Sequence[str], timeout_s: float) -> None:
        if isinstance(launcher, str):
            raise ValueError('launcher must be an argv sequence, not a string')
        self._launcher = tuple(str(part) for part in launcher)
        if not self._launcher or any(not part for part in self._launcher):
            raise ValueError('launcher must be a non-empty argv')
        if ':' in self._launcher:
            raise ValueError('launcher must not contain the MPMD separator ":"')
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not timeout_s > 0.0):
            raise ValueError('timeout_s must be a positive number')
        self._timeout_s = float(timeout_s)
        self._binary = _executable(binary)
        self._sha256 = _sha256(self._binary)
        self._launcher_version = self._probe_launcher()
        self._version: str | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        if self._version is None:
            raise RuntimeError('metadata needs prepare(), warm() or measure() first')
        return {
            'mdtest': {'sha256': self._sha256, 'version': self._version},
            'launcher': {'argv': list(self._launcher),
                         'version': self._launcher_version},
        }

    def prepare(self, shared_paths: Sequence[Path], files: int, size: int,
                log_dir: Path) -> None:
        """Create each shared fixture once, before any measured row."""
        paths = self._paths(shared_paths, 'shared path')
        directory = self._log_dir(log_dir, paths)
        for index, path in enumerate(paths, 1):
            items, _, _ = self._run(
                f'prepare-{index}',
                [('-S', '-C', '-d', str(path), *_options(files, size))],
                ('File creation',), directory,
            )
            if items != files:
                raise RuntimeError(f'mdtest prepared {items} items, expected {files}')

    def warm(self, shared_by_client: Sequence[Sequence[Path]], files: int,
             size: int, log_dir: Path) -> None:
        """Stat and read every client alias once, outside measured rows."""
        clients = self._clients(shared_by_client)
        directory = self._log_dir(
            log_dir, [path for client in clients for path in client]
        )
        for resource in range(1, len(clients[0]) + 1):
            items, _, _ = self._run(
                f'warm-shared-{resource}',
                [self._shared(c[resource - 1], files, size) for c in clients],
                ('File stat', 'File read'), directory,
            )
            if items != files:
                raise RuntimeError(f'mdtest warmed {items} items, expected {files}')

    def measure(self, workspaces: Sequence[Path],
                shared_by_client: Sequence[Sequence[Path]], files: int,
                size: int, log_dir: Path) -> list[Phase]:
        """Measure private and shared file phases and return upstream timings."""
        clients = self._clients(shared_by_client)
        spaces = self._paths(workspaces, 'workspace')
        if len(spaces) != len(clients):
            raise ValueError('workspace count must match the number of clients')
        directory = self._log_dir(
            log_dir, [*spaces, *(path for client in clients for path in client)]
        )
        for space in spaces:
            test_dir = space / _TEST_DIR
            if test_dir.exists() and (not test_dir.is_dir() or any(test_dir.iterdir())):
                raise ValueError(f'private test directory is not empty: {test_dir}')
            test_dir.mkdir(exist_ok=True)
        items, times, ranks = self._run(
            'private', [self._private(space, files, size) for space in spaces],
            _FILE_PHASES, directory,
        )
        if items != files:
            raise RuntimeError(f'mdtest reported {items} items per task, expected {files}')
        phases = [
            Phase(f'private/{label}', len(spaces) * files, times[name], ranks[name])
            for name, label in zip(_FILE_PHASES, ('create', 'stat', 'read', 'remove'))
        ]
        # One resource at a time; every client alias is one rank of the job.
        for resource in range(1, len(clients[0]) + 1):
            items, times, ranks = self._run(
                f'shared-{resource}',
                [self._shared(c[resource - 1], files, size) for c in clients],
                ('File stat', 'File read'), directory,
            )
            if items != files:
                raise RuntimeError(
                    f'mdtest shared-{resource} reported an unexpected item count'
                )
            phases += [
                Phase(f'shared-{resource}/{label}', len(clients) * files,
                      times[name], ranks[name])
                for name, label in (('File stat', 'stat'), ('File read', 'read'))
            ]
        return phases

    def _probe_launcher(self) -> str:
        try:
            done = subprocess.run(
                [*self._launcher, '--version'], capture_output=True, text=True,
                timeout=self._timeout_s,
            )
        except FileNotFoundError as error:
            raise ValueError(f'MPI launcher not found: {self._launcher[0]}') from error
        if done.returncode != 0:
            raise RuntimeError(
                f'MPI launcher --version failed with exit code {done.returncode}'
            )
        text = done.stdout.strip() or done.stderr.strip()
        if not text:
            raise RuntimeError('MPI launcher reported an empty version')
        # MPICH prints "HYDRA build details:" first, so prefer a Version line.
        for line in text.splitlines():
            if re.match(r'^\s*(?:\S+\s+)?version\s*:', line, re.IGNORECASE):
                return line.strip()
        return text

    @staticmethod
    def _shared(path: Path, files: int, size: int) -> tuple[str, ...]:
        return ('-S', '-T', '-E', '-d', str(path), *_options(files, size))

    @staticmethod
    def _private(path: Path, files: int, size: int) -> tuple[str, ...]:
        return ('-F', '-u', '-C', '-T', '-E', '-r', '-d', str(path),
                *_options(files, size))

    def _run(self, label: str, groups: Sequence[tuple[str, ...]],
             phases: Sequence[str],
             log_dir: Path) -> tuple[int, dict[str, float], dict[str, list[float]]]:
        csv_path = log_dir / f'{label}.csv'
        stdout_path = log_dir / f'{label}.stdout.txt'
        stderr_path = log_dir / f'{label}.stderr.txt'
        argv = list(self._launcher)
        for index, group in enumerate(groups):
            if index:
                argv.append(':')
            argv += ['-n', '1', str(self._binary), *group,
                     '--saveRankPerformanceDetails', str(csv_path)]
        with stdout_path.open('w') as out, stderr_path.open('w') as err:
            done = subprocess.run(
                argv, stdout=out, stderr=err, text=True, timeout=self._timeout_s,
            )
        if done.returncode != 0:
            raise RuntimeError(
                f'mdtest run {label} failed with exit code {done.returncode}: '
                f'{_tail(stderr_path) or _tail(stdout_path)}'
            )
        self._version, items, times, ranks = self._parse(
            label, stdout_path.read_text(), csv_path, phases, len(groups)
        )
        return items, times, ranks

    @staticmethod
    def _parse(label: str, stdout: str, csv_path: Path, phases: Sequence[str],
               tasks: int) -> tuple[str, int, dict[str, float], dict[str, list[float]]]:
        match = _BANNER.search(stdout)
        if match is None:
            raise RuntimeError(f'mdtest run {label} did not print its version banner')
        version, launched, nodes = match.group(1), int(match.group(2)), int(match.group(3))
        if version != _VERSION:
            raise RuntimeError(f'mdtest run {label} used mdtest-{version}, need {_VERSION}')
        if launched != tasks or nodes != 1:
            raise RuntimeError(
                f'mdtest run {label} launched {launched} task(s) on {nodes} node(s), '
                f'expected {tasks} on 1'
            )
        rows = [line.split(',') for line in csv_path.read_text().splitlines()
                if line.strip()]
        if (len(rows) < 2 or rows[0] != _HEADER or rows[1][0] != 'all'
                or len(rows[1]) != len(_HEADER)):
            raise RuntimeError(f'mdtest run {label} wrote an unexpected rank CSV')
        if len(rows) - 2 != tasks:
            raise RuntimeError(
                f'mdtest run {label} reported {tasks} tasks but wrote '
                f'{len(rows) - 2} rank rows'
            )
        try:
            items = int(rows[1][1])
            times = {name: float(rows[1][_TIME[name]])
                     for name in _PHASES if rows[1][_TIME[name]]}
        except ValueError as error:
            raise RuntimeError(f'mdtest run {label} wrote an unparsable aggregate row') from error
        ranks: dict[str, list[float]] = {name: [] for name in _FILE_PHASES}
        for position, row in enumerate(rows[2:]):
            if row[0] != str(position) or len(row) < _RANK_ROW:
                raise RuntimeError(f'mdtest run {label} wrote an unexpected rank row')
            try:
                for name in _FILE_PHASES:
                    ranks[name].append(float(row[_TIME[name]] or 0.0))
            except ValueError as error:
                raise RuntimeError(f'mdtest run {label} wrote an unparsable rank time') from error
        for name in phases:
            elapsed = times.get(name, 0.0)
            if (not math.isfinite(elapsed) or elapsed <= 0.0
                    or not all(math.isfinite(v) and v > 0.0 for v in ranks[name])):
                raise RuntimeError(f'mdtest run {label} reported no time for {name}')
        return version, items, times, ranks

    @staticmethod
    def _paths(values: Sequence[Path], what: str) -> list[Path]:
        paths = []
        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute() or '@' in str(path):
                raise ValueError(f'invalid {what}: {path}')
            path = path.resolve()
            if not path.is_dir():
                raise ValueError(f'{what} is not a directory: {path}')
            paths.append(path)
        if len(set(paths)) != len(paths):
            raise ValueError(f'duplicate {what}')
        return paths

    def _clients(self, shared_by_client: Sequence[Sequence[Path]]) -> list[list[Path]]:
        clients = [
            self._paths(paths, f'shared paths for client {index + 1}')
            for index, paths in enumerate(shared_by_client)
        ]
        if not clients:
            raise ValueError('at least one client is required')
        if len({len(paths) for paths in clients}) != 1:
            raise ValueError('every client needs the same number of shared resources')
        return clients

    @staticmethod
    def _log_dir(log_dir: Path, paths: Sequence[Path]) -> Path:
        directory = Path(log_dir).expanduser().resolve()
        for path in paths:
            if directory == path or directory.is_relative_to(path):
                raise ValueError(f'log directory {directory} is inside {path}')
        if directory.exists() and not directory.is_dir():
            raise ValueError(f'log directory is not a directory: {directory}')
        directory.mkdir(parents=True, exist_ok=True)
        return directory
