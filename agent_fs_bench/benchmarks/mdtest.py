"""Fixed-work metadata operations in IOR's mdtest at one task count per run.

Each repeat is one global mdtest (IOR 4.0.0) job of ``tasks`` single-threaded MPI tasks,
placed and pinned by ``agent_fs_bench.cluster``: ``-a POSIX -n total_items/N -i 1`` plus
``-F`` (files) or ``-D`` (directories). The private layout gives every machine one ``-d``
directory plus ``-u``, so each task creates its own tree in that machine's directory;
the shared layout gives all tasks one ``-d`` directory, so across machines the
experimenter must provide a target that every machine sees as one directory. Every run
keeps the same ``total_items`` aggregate work.
No file data is written or read: mdtest's read_bytes keeps its default 0, so its "File
read" phase only opens and closes each file.

mdtest times each phase between barriers. The CSV's aggregate ``all`` row provides
max(time) over tasks; throughput is the fixed total_items / max(time). IOR 4.0.0
overwrites aggregate directory create/stat item counts with rank 0's local counts, so CSV
rate/item fields are not used. Completion is an operation returning, without extra sync
or cache drops between phases.

Before a repeat emits anything, mdtest's launch banner must report the pinned version,
exactly ``N`` tasks and the planned machine count. mdtest's known zero-byte read notice
is expected and any other warning fails the run; a nonzero exit fails immediately, with
no retry. Launcher logs stay in ``ctx.result_dir``; task 0's node returns the rank CSV
through the launcher output. mdtest removes the files and directories it creates; the
empty per-repeat directories (and the precreated test directory on every machine but
task 0's) stay under the run directory.

Only --smoke is exposed on the workload CLI; edit CONFIG for a deliberate experiment.
Targets are mounted externally.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
from pathlib import Path
import re
from typing import Any, Sequence

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import RunContext

MDTEST_VERSION = "4.0.0"
CACHE_STATE = "create-then-access-no-drop"
COMPLETION = "operation-return"
# mdtest's TEST_DIR plus prep_testdir's ".<iteration>-<dir_iter>" for -i 1; mdtest creates it
# only for rank 0, so every private task gets it precreated on its own machine.
TEST_DIR = "test-dir.0-0"

# mdtest_bin must name the same binary on every machine (an absolute path is safest:
# Open MPI does not read shell profiles). total_items is the fixed aggregate work.
CONFIG: dict[str, Any] = {
    "mdtest_bin": "mdtest",
    "tasks": 1,
    "cpus_per_task": 1,
    "oversubscribe": 1,
    "total_items": 65536,
    "repeats": 5,
    "layout": "private",
    "subset": "file",
}

# Reported operation, mdtest's own CSV column, and the subset that runs it.
PHASES = (
    ("file-create", "File creation", "file"),
    ("file-stat", "File stat", "file"),
    ("file-open", "File read", "file"),
    ("file-unlink", "File removal", "file"),
    ("dir-mkdir", "Directory creation", "dir"),
    ("dir-stat", "Directory stat", "dir"),
    ("dir-rename", "Directory rename", "dir"),
    ("dir-rmdir", "Directory removal", "dir"),
)
BANNER = re.compile(
    r"mdtest-(\S+) was launched with (\d+) total task\(s\) on (\d+) node\(s\)"
)
ZERO_READ_WARNING = "Read bytes is 0, thus, a read test will actually just open/close"

FIGURES = [
    Figure('phases', 'tasks', 'ops_per_s', split=('operation', 'layout'),
           xlabel='Tasks', ylabel='Throughput (Kops/s)', yscale=1000, logx=True),
]


class BenchmarkError(RuntimeError):
    pass


def check_banner(text: str, tasks: int, nodes: int, label: str) -> None:
    """Require mdtest's own report of version, task count and machine count."""
    matches = BANNER.findall(text)
    if len(matches) != 1:
        raise BenchmarkError(f"{label}: expected one mdtest launch banner, found {len(matches)}")
    version, launched, reported = matches[0]
    if version != MDTEST_VERSION:
        raise BenchmarkError(
            f"{label}: mdtest reported version {version}, expected {MDTEST_VERSION}"
        )
    if int(launched) != tasks or int(reported) != nodes:
        raise BenchmarkError(
            f"{label}: mdtest launched {launched} task(s) on {reported} node(s), "
            f"expected {tasks} task(s) on {nodes} node(s)"
        )


def check_workload(text: str, subset: str, tasks: int, total_items: int, label: str) -> None:
    """Require the reported task count, total work and object type to match."""
    workload = re.findall(r"^(\d+) tasks, (\d+) (files|directories)$", text, re.MULTILINE)
    expected = (str(tasks), str(total_items), "files" if subset == "file" else "directories")
    if workload != [expected]:
        raise BenchmarkError(f"{label}: mdtest did not report the expected fixed work")


def unexpected_warnings(text: str) -> list[str]:
    """mdtest's own warnings other than the expected zero-byte read notice.

    mdtest writes its log, including warnings, to stdout; the MPI launcher's
    stderr is not mdtest output.
    """
    return sorted({
        line[len("WARNING:"):].strip()
        for line in text.splitlines()
        if line.startswith("WARNING:") and ZERO_READ_WARNING not in line
    })


def read_phases(text: str, subset: str, expected_ops: int) -> list[tuple[str, int, float, float]]:
    """Use aggregate phase times, not IOR 4.0.0's corrupted CSV item counts.

    Every successful phase operates on the fixed total work. Per-rank rows and
    the non-operation Directory read / Tree columns are not measurements here.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise BenchmarkError("empty mdtest CSV")
    columns = {name: position for position, name in enumerate(rows[0])}
    if rows[0][:2] != ["rank", "items"] or len(columns) != len(rows[0]):
        raise BenchmarkError("unexpected mdtest CSV header")
    aggregates = [row for row in rows[1:] if row[:1] == ["all"]]
    if len(aggregates) != 1:
        raise BenchmarkError(
            f"expected exactly one aggregate 'all' row, found {len(aggregates)}"
        )
    row = aggregates[0]
    if len(row) < len(rows[0]):
        raise BenchmarkError("aggregate 'all' row is incomplete")
    phases = []
    for operation, column, phase_subset in PHASES:
        if phase_subset != subset:
            continue
        time_column = f"time-{column}"
        if time_column not in columns:
            raise BenchmarkError(f"no {time_column} column")
        time_text = row[columns[time_column]]
        if not time_text:
            raise BenchmarkError(f"{column} phase did not run")
        elapsed = float(time_text)
        if not math.isfinite(elapsed) or elapsed <= 0:
            raise BenchmarkError(f"{column} has no usable time: {time_text}")
        phases.append((operation, expected_ops, elapsed, expected_ops / elapsed))
    return phases


def launch(ctx: RunContext, slots: Sequence[cluster.Slot], binary: str, options: Sequence[str],
           layout: str, name: str) -> tuple[str, str]:
    """Run one mdtest job; return its stdout and task 0's rank CSV.

    Private tasks share one ``<work>/<node>/<name>`` per machine, with mdtest's test
    directory precreated there (mdtest creates it only for rank 0); shared tasks get
    ``<work>/<name>``.
    """
    capture = Path('/tmp') / f'agent-fs-bench-{ctx.run_id}-{ctx.target.name}-{name}.csv'
    directories, fresh, mkdirs = [], [], []
    for slot in slots:
        if layout == 'private':
            directory = ctx.work_dir / slot.node.name / name
            fresh.append([directory / TEST_DIR])
            mkdirs.append([])
        else:
            directory = ctx.work_dir / name
            fresh.append([])
            mkdirs.append([directory])
        directories.append(directory)
    programs = [[binary, *options, *(['-u'] if layout == 'private' else []), '-d', str(directory),
                 f'--saveRankPerformanceDetails={capture}'] for directory in directories]
    result = cluster.run_native(slots, programs, ctx.result_dir, name, mkdirs=mkdirs, fresh=fresh,
                                within=ctx.target.path, capture=capture)
    if ctx.info.setdefault('mdtest_sha256', result.sha256) != result.sha256:
        raise BenchmarkError(f'{name}: the mdtest binary changed during the run')
    ctx.info.setdefault('placement', {})[name] = result.placement
    return result.stdout, result.capture


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    slots = cluster.allocate(ctx.pool.nodes, args.tasks, args.cpus_per_task, args.oversubscribe)
    tasks = len(slots)
    if args.total_items % tasks or args.total_items // tasks < 2:
        raise BenchmarkError(f"total_items {args.total_items} must divide into at least two "
                             f"items per task for {tasks} tasks")
    items = args.total_items // tasks
    nodes = len({slot.node.name for slot in slots})
    options = ["-a", "POSIX", "-n", str(items), "-i", "1", "-F" if args.subset == "file" else "-D"]
    ctx.info.update(mdtest_version=MDTEST_VERSION, cache_state=CACHE_STATE, completion=COMPLETION)
    for repeat in range(1, args.repeats + 1):
        name = f"{args.subset}-{args.layout}-N{tasks}-rep{repeat}"
        print(f"  {name}: {items} items/task", flush=True)
        stdout, text = launch(ctx, slots, args.mdtest_bin, options, args.layout, name)
        check_banner(stdout, tasks, nodes, name)
        check_workload(stdout, args.subset, tasks, args.total_items, name)
        warnings = unexpected_warnings(stdout)
        if warnings:
            raise BenchmarkError(f"{name}: unexpected mdtest warnings: {'; '.join(warnings)}")
        phases = read_phases(text, args.subset, args.total_items)
        for operation, ops, elapsed, rate in phases:
            ctx.emit("phases", {"operation": operation, "layout": args.layout, "tasks": tasks,
                                "repeat": repeat},
                     {"ops": ops, "elapsed_s": elapsed, "ops_per_s": rate})
        print(f"  {name}: " + ", ".join(f"{operation} {rate:.1f} ops/s"
                                        for operation, _ops, _elapsed, rate in phases), flush=True)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="128 objects in total, two repeats")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    if parsed.smoke:
        settings.update(total_items=128, repeats=2)
    args = argparse.Namespace(**settings, **vars(parsed))
    # A typo would run the wrong layout/subset under its own label, or no phase at all.
    if (args.repeats < 1 or args.layout not in ("private", "shared")
            or args.subset not in ("file", "dir")):
        raise BenchmarkError("layout must be private or shared, subset file or dir, repeats >= 1")
    if args.tasks is not None and (type(args.tasks) is not int or args.tasks < 1
                                   or args.total_items % args.tasks
                                   or args.total_items // args.tasks < 2):
        raise BenchmarkError("total_items must divide evenly into at least two items per task")
    return args
