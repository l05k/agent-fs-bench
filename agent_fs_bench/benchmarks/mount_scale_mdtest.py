"""Native mdtest phases for mount-scale, using the original IOR 4.0.0 binary.

This module builds one private create job (without per-file fsync) or a
create-then-read job with per-file fsync (`-y`). Both keep native phase times and rank CSV;
removal runs outside the reported create/read phases.

| Symbol | Definition |
| --- | --- |
| N | Client tasks of one global MPI job; the rank is the appfile line |
| F | `files` per task and phase |
| T_p | Aggregate phase time: mdtest's `all` row, the maximum task time including the end barrier |
| t_{p,r} | Rank r's phase time before the end barrier |

Every phase is one global Open MPI 4.1 MPMD job (no CPU binding). Rank r's ``-d`` is
client r's own alias, created by its machine's lifecycle worker, so every access goes
through that client's mount. mdtest runs unchanged: one ``MPI_COMM_WORLD``, one
``-d`` per task, its own barriers and rank CSV.
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

from .mount_scale_budget import read_placements

VERSION = '4.0.0'
READ = 'read'
CREATE_WRITE = 'create-write'
# mdtest's mdtest_test_num_t order; the rank CSV header repeats every name.
_PHASES = (
    'Directory creation', 'Directory stat', 'Directory read', 'Directory rename',
    'Directory removal', 'File creation', 'File stat', 'File read',
    'File removal', 'Tree creation', 'Tree removal',
)
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
    cgroup: Path | None = None  # Site-created shared workload/FUSE budget, if configured.


@dataclass(frozen=True)
class Timings:
    items: int  # Rank 0's final items value; not sufficient proof of completion.
    operations: dict[str, int]  # Actual aggregate operations, reconstructed from native CSV.
    rank_operations: dict[str, list[int]]
    phase_s: dict[str, float]  # T_p
    rank_s: dict[str, list[float]]  # t_{p,r}, rank order


@dataclass(frozen=True)
class Phase:
    name: str
    operations: int
    elapsed_s: float
    rank_elapsed_s: list[float]


def create_job(path: Path, files: int, size: int, stonewall_seconds: int | None) -> tuple[str, ...]:
    """Create/write/close without fsync; native removal follows outside create timing."""
    arguments = ('-F', '-u', '-C', '-r', '-w', str(size), '-e', str(size),
                 '-d', str(path), '-n', str(files), '-i', '1', '--warningAsErrors')
    return arguments if stonewall_seconds is None else (*arguments, '-W', str(stonewall_seconds))


def native_io_job(path: Path, files: int, size: int) -> tuple[str, ...]:
    """Native create then read, with a per-file fsync in the create phase."""
    return ('-F', '-u', '-C', '-E', '-r', '-y', '-d', str(path), '-n', str(files),
            '-w', str(size), '-e', str(size), '-i', '1', '--warningAsErrors')


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


def _operations(label: str, rate: str, seconds: str) -> int:
    """IOR prints rate=completed/time and time with %.10e (11 significant digits)."""
    product = _seconds(label + ' rate', rate) * _seconds(label + ' time', seconds)
    count = round(product)
    tolerance = max(1e-6, product * 2e-10)
    assert tolerance < 0.5 and count > 0 and abs(product - count) <= tolerance, \
        f'{label} completed operations: expected an unambiguous integer, measured {product}'
    return count


def read_timings(label: str, text: str, phases: Sequence[str], tasks: int) -> Timings:
    """T_p from the `all` row and t_{p,r} from the rank rows, for `phases` only."""
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    assert rows[0] == _HEADER, f'{label} CSV header: expected mdtest {VERSION}, measured {rows[0]}'
    assert rows[1][0] == 'all', f'{label} first CSV row: expected all, measured {rows[1][0]}'
    ranks = rows[2:]
    assert [row[0] for row in ranks] == [str(rank) for rank in range(tasks)], \
        f'{label} rank rows: expected {tasks}, measured {len(ranks)}'
    operations = {name: _operations(f'{label} {name}', rows[1][_TIME[name] - 1],
                                   rows[1][_TIME[name]]) for name in phases}
    rank_operations = {
        name: [_operations(f'{label} rank {row[0]} {name}', row[_TIME[name] - 1],
                           row[_TIME[name]]) for row in ranks] for name in phases}
    for name in phases:
        assert operations[name] == sum(rank_operations[name]), \
            f'{label} {name} completed operations: aggregate {operations[name]}, ' \
            f'rank sum {sum(rank_operations[name])}'
    return Timings(
        items=int(rows[1][1]),
        operations=operations,
        rank_operations=rank_operations,
        phase_s={name: _seconds(f'{label} {name}', rows[1][_TIME[name]]) for name in phases},
        rank_s={name: [_seconds(f'{label} rank {row[0]} {name}', row[_TIME[name]])
                       for row in ranks] for name in phases})


# Jobs ----------------------------------------------------------------------------------------

def run_job(launcher: cluster.Launcher, binary: str, tag: str, label: str, ranks: Sequence[Rank],
            groups: Sequence[Sequence[str]], phases: Sequence[str], log_dir: Path,
            files: int, timeout_s: float, startup_timeout_s: float) -> Timings:
    """Run one global MPMD job and return its validated timings."""
    capture = Path('/tmp') / f'agent-fs-bench-{tag}-{label}.csv'
    placement = tuple(rank.node for rank in ranks)
    directories: dict[cluster.Node, list[Path]] = {}
    for rank in ranks:
        directories.setdefault(rank.node, []).append(rank.workspace / _TEST_DIR)
    cluster.make_directories(launcher, directories)
    programs = []
    for rank, group in zip(ranks, groups, strict=True):
        arguments = [binary, *group, '--saveRankPerformanceDetails', str(capture)]
        if rank.cgroup is None:
            programs.append([rank.node.python, '-u', '-m', 'agent_fs_bench.cluster',
                             'rank-exec', *arguments])
        else:
            programs.append([rank.node.python, '-u', '-m',
                             'agent_fs_bench.benchmarks.mount_scale_runner', 'mdtest',
                             '--cgroup', str(rank.cgroup), '--', *arguments])
    run = cluster.run_program(placement, launcher, programs, log_dir, label,
                              timeout_s=timeout_s, startup_timeout_s=startup_timeout_s)
    cluster.validate_exit(run)
    check_banner(label, run.stdout, len(ranks))
    read_placements(run.stdout, [rank.cgroup for rank in ranks])
    counts = re.findall(r'^(\d+) tasks, (\d+) files$', run.stdout, re.MULTILINE)
    expected = [(str(len(ranks)), str(len(ranks) * files))]
    assert counts == expected, f'{label} workload: expected {expected}, measured {counts}'
    text = cluster.fetch_file(placement[0], launcher, capture)
    (log_dir / 'rank-performance.csv').write_text(text, encoding='utf-8')
    timings = read_timings(label, text, phases, len(ranks))
    for phase in phases:
        expected_ops = len(ranks) * files
        assert timings.operations[phase] == expected_ops and \
            timings.rank_operations[phase] == [files] * len(ranks), \
            f'{label} {phase} incomplete: expected {expected_ops} operations ' \
            f'({files} per rank), measured {timings.operations[phase]} ' \
            f'({timings.rank_operations[phase]})'
    assert timings.items == files, f'{label} items per task: expected {files}, ' \
                                   f'measured {timings.items}'
    return timings


@dataclass(frozen=True)
class Mdtest:
    """Upstream mdtest 4.0.0 file phases of one run and target."""

    binary: str
    launcher: cluster.Launcher
    tag: str  # Unique per run and target: names rank 0's CSV file.
    timeout_s: float  # One MPI job; run_program kills the launcher group on expiry.
    startup_timeout_s: float

    def job(self, label: str, ranks: Sequence[Rank], groups: Sequence[Sequence[str]],
            phases: Sequence[str], log_dir: Path, files: int) -> Timings:
        return run_job(self.launcher, self.binary, self.tag, label, ranks, groups, phases,
                       log_dir, files, self.timeout_s, self.startup_timeout_s)

    def create_timings(self, ranks: Sequence[Rank], files: int, size: int,
                       stonewall_seconds: int | None, log_dir: Path) -> Timings:
        """Native create without fsync; a truncated stonewall job fails count validation."""
        return self.job('private-create', ranks,
                        [create_job(rank.workspace, files, size, stonewall_seconds)
                         for rank in ranks], ('File creation',), log_dir, files)

    def native_io_timings(self, ranks: Sequence[Rank], files: int, size: int,
                          log_dir: Path) -> Timings:
        """Native create then read, with per-file fsync in the create phase."""
        return self.job('private-io', ranks,
                        [native_io_job(rank.workspace, files, size) for rank in ranks],
                        ('File creation', 'File read'), log_dir, files)


