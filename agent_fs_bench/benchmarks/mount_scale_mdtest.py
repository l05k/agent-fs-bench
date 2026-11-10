"""Mount-scale file phases with the original IOR 4.0.0 mdtest binary.

| Symbol | Definition |
| --- | --- |
| N | Client tasks of one global MPI job; the rank is the appfile line |
| F | `files` per task and phase |
| T_p | Aggregate phase time: mdtest's `all` row, the maximum task time including the end barrier |
| t_{p,r} | Rank r's phase time before the end barrier |

Every phase is one global Open MPI 4.1 MPMD job (no CPU binding). Rank r's ``-d`` is
client r's own alias, created by its machine's lifecycle worker, so every access goes
through that client's mount. mdtest runs unchanged: one ``MPI_COMM_WORLD``, one ``-d``
per task (mdtest rejects several paths with ``-S``), its own barriers and rank CSV.
Private phases need ``test-dir.0-0`` in each workspace, which mdtest creates only for
low ranks, so the controller creates it first. Timing comes only from mdtest's CSV;
command wall time is never a metric and rates are never summed per machine.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import math
from pathlib import Path
import re
from typing import Sequence

from agent_fs_bench import cluster

VERSION = '4.0.0'
# mdtest's mdtest_test_num_t order; the rank CSV header repeats every name.
_PHASES = (
    'Directory creation', 'Directory stat', 'Directory read', 'Directory rename',
    'Directory removal', 'File creation', 'File stat', 'File read',
    'File removal', 'Tree creation', 'Tree removal',
)
FILE_PHASES = ('File creation', 'File stat', 'File read', 'File removal')
SHARED_PHASES = ('File stat', 'File read')
_HEADER = ['rank', 'items'] + [f'{kind}-{name}' for name in _PHASES
                               for kind in ('rate', 'time')]
# Phase time column; rank rows share the layout up to the tree phases.
_TIME = {name: 3 + 2 * index for index, name in enumerate(_PHASES)}
# Open MPI may prefix its own lines, so the banner is matched anywhere in a line.
_BANNER = re.compile(
    r'mdtest-(\S+) was launched with (\d+) total task\(s\) on (\d+) node\(s\)'
)
_TEST_DIR = 'test-dir.0-0'


@dataclass(frozen=True)
class Rank:
    """One mdtest task: its machine and its client aliases on that machine."""

    node: cluster.Node
    workspace: Path  # Private test directory parent; exists on that machine only.
    shared: tuple[Path, ...]


@dataclass(frozen=True)
class Timings:
    items: int  # Items per task reported by mdtest.
    phase_s: dict[str, float]  # T_p
    rank_s: dict[str, list[float]]  # t_{p,r}, rank order


@dataclass(frozen=True)
class Phase:
    name: str
    operations: int
    elapsed_s: float
    rank_elapsed_s: list[float]


def _options(files: int, size: int) -> tuple[str, ...]:
    return ('-F', '-n', str(files), '-w', str(size), '-e', str(size), '-i', '1',
            '--warningAsErrors')


def shared_job(path: Path, files: int, size: int) -> tuple[str, ...]:
    return ('-S', '-T', '-E', '-d', str(path), *_options(files, size))


def private_job(path: Path, files: int, size: int) -> tuple[str, ...]:
    return ('-F', '-u', '-C', '-T', '-E', '-r', '-d', str(path), *_options(files, size))


# Validation and parsing (pure) ---------------------------------------------------------------

def check_banner(label: str, stdout: str, tasks: int) -> None:
    match = _BANNER.search(stdout)
    assert match, f'{label} mdtest banner: expected one, measured none'
    assert match.group(1) == VERSION, \
        f'{label} mdtest version: expected {VERSION}, measured {match.group(1)}'
    assert int(match.group(2)) == tasks, \
        f'{label} tasks: expected {tasks}, measured {match.group(2)}'


def _seconds(label: str, text: str) -> float:
    value = float(text)
    assert math.isfinite(value) and value > 0, f'{label}: expected time > 0, measured {text!r}'
    return value


def read_timings(label: str, text: str, phases: Sequence[str], tasks: int) -> Timings:
    """T_p from the `all` row and t_{p,r} from the rank rows, for `phases` only."""
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    assert rows[0] == _HEADER, f'{label} CSV header: expected mdtest {VERSION}, measured {rows[0]}'
    assert rows[1][0] == 'all', f'{label} first CSV row: expected all, measured {rows[1][0]}'
    ranks = rows[2:]
    assert [row[0] for row in ranks] == [str(rank) for rank in range(tasks)], \
        f'{label} rank rows: expected {tasks}, measured {len(ranks)}'
    return Timings(
        items=int(rows[1][1]),
        phase_s={name: _seconds(f'{label} {name}', rows[1][_TIME[name]]) for name in phases},
        rank_s={name: [_seconds(f'{label} rank {row[0]} {name}', row[_TIME[name]])
                       for row in ranks] for name in phases})


def phases_of(prefix: str, labels: Sequence[str], names: Sequence[str], timings: Timings,
              operations: int) -> list[Phase]:
    return [Phase(f'{prefix}/{label}', operations, timings.phase_s[name], timings.rank_s[name])
            for label, name in zip(labels, names)]


# Jobs ----------------------------------------------------------------------------------------

def run_job(launcher: cluster.Launcher, binary: str, tag: str, label: str, ranks: Sequence[Rank],
            groups: Sequence[Sequence[str]], phases: Sequence[str], log_dir: Path,
            files: int, private: bool = False) -> Timings:
    """Run one global MPMD job and return its validated timings."""
    capture = Path('/tmp') / f'agent-fs-bench-{tag}-{label}.csv'
    placement = tuple(rank.node for rank in ranks)
    if private:
        directories: dict[cluster.Node, list[Path]] = {}
        for rank in ranks:
            directories.setdefault(rank.node, []).append(rank.workspace / _TEST_DIR)
        cluster.make_directories(launcher, directories)
    programs = [[binary, *group, '--saveRankPerformanceDetails', str(capture)]
                for group in groups]
    run = cluster.run_program(placement, launcher, programs, log_dir, label)
    cluster.validate_exit(run)
    check_banner(label, run.stdout, len(ranks))
    timings = read_timings(label, cluster.fetch_file(placement[0], launcher, capture),
                           phases, len(ranks))
    assert timings.items == files, f'{label} items per task: expected {files}, ' \
                                   f'measured {timings.items}'
    return timings


@dataclass(frozen=True)
class Mdtest:
    """Upstream mdtest 4.0.0 file phases of one run and target."""

    binary: str
    launcher: cluster.Launcher
    tag: str  # Unique per run and target: names rank 0's CSV file.

    def job(self, label: str, ranks: Sequence[Rank], groups: Sequence[Sequence[str]],
            phases: Sequence[str], log_dir: Path, files: int, private: bool = False) -> Timings:
        return run_job(self.launcher, self.binary, self.tag, label, ranks, groups, phases,
                       log_dir, files, private)

    def prepare(self, node: cluster.Node, shared_paths: Sequence[Path], files: int, size: int,
                log_dir: Path) -> None:
        """Create each shared fixture once with one task on `node`, before measurement."""
        for index, path in enumerate(shared_paths, 1):
            self.job(f'prepare-{index}', [Rank(node, path, ())],
                     [('-S', '-C', '-d', str(path), *_options(files, size))],
                     ('File creation',), log_dir, files)

    def warm(self, ranks: Sequence[Rank], files: int, size: int, log_dir: Path) -> None:
        """Warm-up: stat and read every client alias once; the timings are discarded."""
        for resource in range(len(ranks[0].shared)):
            self.job(f'warm-shared-{resource + 1}', ranks,
                     [shared_job(rank.shared[resource], files, size) for rank in ranks],
                     SHARED_PHASES, log_dir, files)

    def measure(self, ranks: Sequence[Rank], files: int, size: int, log_dir: Path) -> list[Phase]:
        """Sampling: the private job, then one job per shared resource."""
        operations = len(ranks) * files
        timings = self.job('private', ranks,
                           [private_job(rank.workspace, files, size) for rank in ranks],
                           FILE_PHASES, log_dir, files, private=True)
        phases = phases_of('private', ('create', 'stat', 'read', 'remove'), FILE_PHASES,
                           timings, operations)
        for resource in range(len(ranks[0].shared)):
            label = f'shared-{resource + 1}'
            timings = self.job(label, ranks,
                               [shared_job(rank.shared[resource], files, size) for rank in ranks],
                               SHARED_PHASES, log_dir, files)
            phases += phases_of(label, ('stat', 'read'), SHARED_PHASES, timings, operations)
        return phases
