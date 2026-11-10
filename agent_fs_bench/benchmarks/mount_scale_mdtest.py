"""Measure mount scaling with the original IOR 4.0.0 mdtest binary.

Every phase is one global Open MPI 4.1 MPMD job launched by ``agent_fs_bench.cluster``,
with one application context per client task pinned to its machine and logical CPU. The
cluster wrapper verifies the actual hostname, MPI rank and single-CPU affinity, requires
the task's client aliases to exist on its own machine, prints one placement proof with the
binary's SHA-256, and only then execs mdtest. Placement is therefore proved per task, and
this module never resolves or stats a remote path on the controller.

mdtest runs unchanged upstream 4.0.0: one global ``MPI_COMM_WORLD``, exactly one ``-d``
path per task (mdtest rejects several paths with ``-S`` and without ``-u``), its own phase
barriers, and its own rank CSV. Every task accesses the same task-independent
``mdtest.shared.<n>`` names through its own client alias. Private phases create each
task's ``test-dir.0-0`` if absent on the machine that owns the workspace, because mdtest
creates it only for ranks below ``path_count``; each task then creates
``mdtest_tree.<rank>.0``.

Timing comes only from mdtest's rank CSV: the ``all`` row is the aggregate (maximum task
time including the phase-end barrier), each ``<rank>`` row is that task's time before the
barrier. Command wall time is never a metric and rates are never summed in Python. Global
rank 0 writes the CSV on its own machine; the wrapper returns it through the launcher
output, so no machine needs a shared result directory.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Sequence

from agent_fs_bench import cluster

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
# Open MPI may prefix or interleave its own daemon/launcher lines, so the
# upstream banner is matched by its distinctive text rather than at line start.
_BANNER = re.compile(
    r'mdtest-(\S+) was launched with (\d+) total task\(s\) on (\d+) node\(s\)'
)
# mdtest's TEST_DIR plus prep_testdir's ".<iteration>-<dir_iter>" for -i 1.
_TEST_DIR = 'test-dir.0-0'


@dataclass(frozen=True)
class Phase:
    name: str
    operations: int
    elapsed_s: float
    rank_elapsed_s: list[float]


@dataclass(frozen=True)
class Rank:
    """One mdtest task: its placement and its client aliases on that machine."""

    slot: cluster.Slot
    workspace: Path  # Private test directory parent; exists on the slot's machine only.
    shared: tuple[Path, ...]


def _options(files: int, size: int) -> tuple[str, ...]:
    return ('-F', '-n', str(files), '-w', str(size), '-e', str(size), '-i', '1',
            '--warningAsErrors')


class Mdtest:
    """Run upstream mdtest 4.0.0 file phases and report their upstream timings."""

    def __init__(self, binary: str, timeout_s: float, tag: str) -> None:
        self._binary = binary
        self._timeout_s = float(timeout_s)
        self._tag = tag  # Unique per run and target: names task 0's CSV file.
        self._jobs = 0
        self._sha256: str | None = None
        self._version: str | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        """Version and digest only: no addresses, argv or SSH credentials."""
        return {'mdtest': {'sha256': self._sha256, 'version': self._version}}

    def prepare(self, slot: cluster.Slot, shared_paths: Sequence[Path], files: int, size: int,
                log_dir: Path) -> None:
        """Create each shared fixture once with one task on `slot`, before measurement."""
        for index, path in enumerate(shared_paths, 1):
            items, _, _, _ = self._job(
                f'prepare-{index}', [Rank(slot, path, ())],
                [('-S', '-C', '-d', str(path), *_options(files, size))],
                ('File creation',), log_dir,
            )
            if items != files:
                raise RuntimeError(f'mdtest prepared {items} items, expected {files}')

    def warm(self, ranks: Sequence[Rank], files: int, size: int, log_dir: Path) -> None:
        """Stat and read every client alias once, outside measured rows."""
        for resource in range(len(ranks[0].shared)):
            items, _, _, _ = self._job(
                f'warm-shared-{resource + 1}', ranks,
                [self._shared(rank.shared[resource], files, size) for rank in ranks],
                ('File stat', 'File read'), log_dir,
            )
            if items != files:
                raise RuntimeError(f'mdtest warmed {items} items, expected {files}')

    def measure(self, ranks: Sequence[Rank], files: int, size: int, log_dir: Path
                ) -> tuple[list[Phase], dict[str, list[dict[str, Any]]]]:
        """Return native phases and verified placements for every measured job."""
        items, times, per_rank, private_placement = self._job(
            'private', ranks, [self._private(rank.workspace, files, size) for rank in ranks],
            _FILE_PHASES, log_dir, private=True,
        )
        if items != files:
            raise RuntimeError(f'mdtest reported {items} items per task, expected {files}')
        placements = {'private': private_placement}
        phases = [
            Phase(f'private/{label}', len(ranks) * files, times[name], per_rank[name])
            for name, label in zip(_FILE_PHASES, ('create', 'stat', 'read', 'remove'))
        ]
        # One resource at a time; every client alias is one task of the same job.
        for resource in range(len(ranks[0].shared)):
            items, times, per_rank, shared_placement = self._job(
                f'shared-{resource + 1}', ranks,
                [self._shared(rank.shared[resource], files, size) for rank in ranks],
                ('File stat', 'File read'), log_dir,
            )
            if items != files:
                raise RuntimeError(
                    f'mdtest shared-{resource + 1} reported an unexpected item count'
                )
            placements[f'shared-{resource + 1}'] = shared_placement
            phases += [
                Phase(f'shared-{resource + 1}/{label}', len(ranks) * files,
                      times[name], per_rank[name])
                for name, label in (('File stat', 'stat'), ('File read', 'read'))
            ]
        return phases, placements

    @staticmethod
    def _shared(path: Path, files: int, size: int) -> tuple[str, ...]:
        return ('-S', '-T', '-E', '-d', str(path), *_options(files, size))

    @staticmethod
    def _private(path: Path, files: int, size: int) -> tuple[str, ...]:
        return ('-F', '-u', '-C', '-T', '-E', '-r', '-d', str(path),
                *_options(files, size))

    def _job(self, label: str, ranks: Sequence[Rank], groups: Sequence[Sequence[str]],
             phases: Sequence[str], log_dir: Path, *, private: bool = False
             ) -> tuple[int, dict[str, float], dict[str, list[float]], list[dict[str, Any]]]:
        """Run one global MPMD job and validate its placement and timings."""
        if len(ranks) != len(groups) or not ranks:
            raise ValueError('every task needs exactly one mdtest argument group')
        if len({len(rank.shared) for rank in ranks}) != 1:
            raise ValueError('every task needs the same number of shared resources')
        self._jobs += 1
        capture = Path('/tmp') / f'agent-fs-bench-{self._tag}-{self._jobs:04d}-{label}.csv'
        slots = [rank.slot for rank in ranks]
        programs = [[self._binary, *group, '--saveRankPerformanceDetails', str(capture)]
                    for group in groups]
        result = cluster.run_native(
            slots, programs, log_dir, label,
            exists=[[rank.workspace, *rank.shared] for rank in ranks],
            mkdirs=[[rank.workspace / _TEST_DIR] if private else [] for rank in ranks],
            capture=capture, timeout_s=self._timeout_s,
        )
        if self._sha256 is None:
            self._sha256 = result.sha256
        elif result.sha256 != self._sha256:
            raise RuntimeError(f'mdtest run {label} used a different mdtest binary')
        self._version, items, times, per_rank = self._parse(
            label, result.stdout, result.capture, phases, len(ranks))
        return items, times, per_rank, result.placement

    @staticmethod
    def _parse(label: str, stdout: str, text: str, phases: Sequence[str],
               tasks: int) -> tuple[str, int, dict[str, float], dict[str, list[float]]]:
        match = _BANNER.search(stdout)
        if match is None:
            raise RuntimeError(f'mdtest run {label} did not print its version banner')
        version, launched = match.group(1), int(match.group(2))
        if version != _VERSION:
            raise RuntimeError(f'mdtest run {label} used mdtest-{version}, need {_VERSION}')
        if launched != tasks:
            raise RuntimeError(
                f'mdtest run {label} launched {launched} task(s), expected {tasks}'
            )
        rows = [row for row in csv.reader(io.StringIO(text)) if row]
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
        per_rank: dict[str, list[float]] = {name: [] for name in _FILE_PHASES}
        for position, row in enumerate(rows[2:]):
            if row[0] != str(position) or len(row) < _RANK_ROW:
                raise RuntimeError(f'mdtest run {label} wrote an unexpected rank row')
            try:
                for name in _FILE_PHASES:
                    per_rank[name].append(float(row[_TIME[name]] or 0.0))
            except ValueError as error:
                raise RuntimeError(f'mdtest run {label} wrote an unparsable rank time') from error
        for name in phases:
            elapsed = times.get(name, 0.0)
            if (not math.isfinite(elapsed) or elapsed <= 0.0
                    or not all(math.isfinite(v) and v > 0.0 for v in per_rank[name])):
                raise RuntimeError(f'mdtest run {label} reported no time for {name}')
        return version, items, times, per_rank
