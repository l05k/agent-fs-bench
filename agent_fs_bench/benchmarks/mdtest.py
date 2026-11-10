"""Fixed-work metadata operations in IOR's mdtest at one task count per run.

| Symbol | Definition |
| --- | --- |
| N | `tasks`: single-threaded MPI tasks of one mdtest job, placed by `agent_fs_bench.cluster` |
| W | `total_items`: fixed aggregate objects per phase; each task handles W/N |
| T_p | Phase time: mdtest's aggregate `all` row, the maximum task time including the end barrier |
| t_{p,r} | Rank r's time for phase p before the end barrier (per-rank rows) |
| X_p | Throughput `ops_per_s = W / T_p` |

Each repeat is one global mdtest (IOR 4.0.0) job: ``-a POSIX -n W/N -i 1`` plus ``-F``
(files) or ``-D`` (directories). The private layout gives every machine one ``-d``
directory plus ``-u``, so each task creates its own tree; the shared layout gives all
tasks one ``-d`` directory, which across machines must be one shared directory. No file
data is written or read ("File read" only opens and closes).

Warm-up, then sampling: ``warmup_repeats`` identical launches run first, are validated
like measured ones, and their timings are discarded. Every measured repeat therefore
starts from the same cache state: target mounted, directories, client caches and MPI
launch paths already exercised by an identical job, no cache drop. mdtest removes its
own objects, so each repeat still creates fresh files.

IOR 4.0.0 overwrites aggregate directory create/stat item counts with rank 0's local
counts, so CSV rate/item fields are never used; X_p always divides the fixed W.

Tables: `phases` (one row per repeat × operation: T_p, X_p) and `ranks` (one row per
repeat × operation × rank: t_{p,r}).
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
CACHE_STATE = "after-warmup-create-then-access-no-drop"
COMPLETION = "operation-return"
# mdtest creates its test directory only for rank 0; every private machine gets it first.
TEST_DIR = "test-dir.0-0"

# mdtest_bin must name the same binary on every machine (an absolute path is safest:
# Open MPI does not read shell profiles). total_items is the fixed aggregate work W.
CONFIG: dict[str, Any] = {
    "mdtest_bin": "mdtest",
    "tasks": 1,
    "total_items": 65536,
    "warmup_repeats": 1,  # Discarded launches before the measured repeats.
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


# Validation ----------------------------------------------------------------------------------

def check_banner(text: str, tasks: int, label: str) -> None:
    """mdtest's own version and task count; its node count counts shared-memory domains."""
    matches = BANNER.findall(text)
    assert len(matches) == 1, f"{label} launch banners: expected 1, measured {len(matches)}"
    version, launched, _domains = matches[0]
    assert version == MDTEST_VERSION, \
        f"{label} mdtest version: expected {MDTEST_VERSION}, measured {version}"
    assert int(launched) == tasks, f"{label} tasks: expected {tasks}, measured {launched}"


def check_workload(text: str, subset: str, tasks: int, total_items: int, label: str) -> None:
    workload = re.findall(r"^(\d+) tasks, (\d+) (files|directories)$", text, re.MULTILINE)
    expected = [(str(tasks), str(total_items), "files" if subset == "file" else "directories")]
    assert workload == expected, f"{label} work: expected {expected}, measured {workload}"


def unexpected_warnings(text: str) -> list[str]:
    """mdtest's own warnings (on stdout) other than the expected zero-byte read notice."""
    return sorted({
        line[len("WARNING:"):].strip()
        for line in text.splitlines()
        if line.startswith("WARNING:") and ZERO_READ_WARNING not in line
    })


def validate(stdout: str, subset: str, tasks: int, total_items: int, label: str) -> None:
    check_banner(stdout, tasks, label)
    check_workload(stdout, subset, tasks, total_items, label)
    warnings = unexpected_warnings(stdout)
    assert not warnings, f"{label} mdtest warnings: expected none, measured {warnings}"


# Parsing and metrics (pure) ------------------------------------------------------------------

def _seconds(text: str, label: str) -> float:
    assert text, f"{label}: expected a phase time, measured an empty cell"
    value = float(text)
    assert math.isfinite(value) and value > 0, f"{label}: expected time > 0, measured {text}"
    return value


def _csv(text: str) -> tuple[dict[str, int], list[list[str]]]:
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    assert rows and rows[0][:2] == ["rank", "items"], \
        f"mdtest CSV header: expected rank,items,..., measured {rows[:1]}"
    return {name: position for position, name in enumerate(rows[0])}, rows[1:]


def subset_phases(subset: str) -> list[tuple[str, str]]:
    return [(operation, column) for operation, column, kind in PHASES if kind == subset]


def read_phase_times(text: str, subset: str) -> dict[str, float]:
    """T_p of every operation of `subset` from the aggregate `all` row."""
    columns, rows = _csv(text)
    aggregates = [row for row in rows if row[0] == "all"]
    assert len(aggregates) == 1, f"aggregate 'all' rows: expected 1, measured {len(aggregates)}"
    return {operation: _seconds(aggregates[0][columns[f"time-{column}"]], f"{column} time")
            for operation, column in subset_phases(subset)}


def read_rank_times(text: str, subset: str, tasks: int) -> dict[str, list[float]]:
    """t_{p,r} of every operation of `subset`, in rank order."""
    columns, rows = _csv(text)
    ranks = [row for row in rows if row[0] != "all"]
    assert [row[0] for row in ranks] == [str(rank) for rank in range(tasks)], \
        f"rank rows: expected 0..{tasks - 1}, measured {[row[0] for row in ranks]}"
    return {operation: [_seconds(row[columns[f"time-{column}"]], f"rank {row[0]} {column} time")
                        for row in ranks]
            for operation, column in subset_phases(subset)}


def throughput(total_items: int, elapsed_s: float) -> float:
    """X_p = W / T_p."""
    return total_items / elapsed_s


def read_phases(text: str, subset: str, total_items: int) -> list[tuple[str, int, float, float]]:
    """(operation, W, T_p, X_p) for every operation of `subset`."""
    return [(operation, total_items, elapsed, throughput(total_items, elapsed))
            for operation, elapsed in read_phase_times(text, subset).items()]


# Run and output ------------------------------------------------------------------------------

def launch(ctx: RunContext, placement: cluster.Placement, binary: str, options: Sequence[str],
           layout: str, name: str) -> tuple[str, str]:
    """Run one mdtest job; return its stdout and rank 0's CSV."""
    launcher = ctx.pool.launcher
    capture = Path('/tmp') / f'agent-fs-bench-{ctx.run_id}-{ctx.target.name}-{name}.csv'
    directory = {node: ctx.work_dir / node.name / name if layout == 'private'
                 else ctx.work_dir / name for node in cluster.machines(placement)}
    cluster.make_directories(launcher, {
        node: [path / TEST_DIR if layout == 'private' else path]
        for node, path in directory.items()})
    programs = [[binary, *options, *(['-u'] if layout == 'private' else []),
                 '-d', directory[node], f'--saveRankPerformanceDetails={capture}']
                for node in placement]
    run = cluster.run_program(placement, launcher, programs, ctx.result_dir, name)
    cluster.validate_exit(run)
    return run.stdout, cluster.fetch_file(placement[0], launcher, capture)


def binary_info(launcher: cluster.Launcher, placement: cluster.Placement,
                binary: str) -> dict[str, Any]:
    digests = cluster.binary_digests(cluster.machines(placement), launcher, binary)
    return {'mdtest_sha256': cluster.validate_binary(binary, digests),
            'placement': cluster.placement_record(placement)}


def warm_up(ctx: RunContext, placement: cluster.Placement, binary: str, options: Sequence[str],
            layout: str, subset: str, total_items: int, label: str, count: int) -> None:
    """Warm-up phase: `count` full launches, validated, timings discarded."""
    for number in range(1, count + 1):
        name = f"{label}-warmup{number}"
        stdout, _text = launch(ctx, placement, binary, options, layout, name)
        validate(stdout, subset, len(placement), total_items, name)


def write_repeat(ctx: RunContext, keys: dict[str, Any], placement: cluster.Placement,
                 phases: list[tuple[str, int, float, float]],
                 ranks: dict[str, list[float]]) -> None:
    for operation, ops, elapsed, rate in phases:
        ctx.emit("phases", {"operation": operation, **keys},
                 {"ops": ops, "elapsed_s": elapsed, "ops_per_s": rate})
        for sample, elapsed_rank in enumerate(ranks[operation]):
            ctx.emit("ranks", {"operation": operation, **keys, "sample": sample,
                               "host": placement[sample].name}, {"elapsed_s": elapsed_rank})


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    placement = cluster.place(ctx.pool.nodes, args.tasks)
    tasks = len(placement)
    items = args.total_items // tasks
    options = ["-a", "POSIX", "-n", str(items), "-i", "1", "-F" if args.subset == "file" else "-D"]
    ctx.info.update(mdtest_version=MDTEST_VERSION, cache_state=CACHE_STATE, completion=COMPLETION,
                    **binary_info(ctx.pool.launcher, placement, args.mdtest_bin))
    label = f"{args.subset}-{args.layout}-N{tasks}"
    warm_up(ctx, placement, args.mdtest_bin, options, args.layout, args.subset,
            args.total_items, label, args.warmup_repeats)
    for repeat in range(1, args.repeats + 1):
        name = f"{label}-rep{repeat}"
        print(f"  {name}: {items} items/task", flush=True)
        stdout, text = launch(ctx, placement, args.mdtest_bin, options, args.layout, name)
        validate(stdout, args.subset, tasks, args.total_items, name)
        phases = read_phases(text, args.subset, args.total_items)
        ranks = read_rank_times(text, args.subset, tasks)
        write_repeat(ctx, {"layout": args.layout, "tasks": tasks, "repeat": repeat},
                     placement, phases, ranks)
        print(f"  {name}: " + ", ".join(f"{operation} {rate:.1f} ops/s"
                                        for operation, _ops, _elapsed, rate in phases), flush=True)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="128 objects in total, two repeats")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    if parsed.smoke:
        settings.update(total_items=128, warmup_repeats=1, repeats=2)
    args = argparse.Namespace(**settings, **vars(parsed))
    assert args.repeats >= 1, f"repeats: expected >= 1, measured {args.repeats}"
    assert args.warmup_repeats >= 1, \
        f"warmup_repeats: expected >= 1, measured {args.warmup_repeats}"
    assert args.layout in ("private", "shared"), \
        f"layout: expected private or shared, measured {args.layout!r}"
    assert args.subset in ("file", "dir"), f"subset: expected file or dir, measured {args.subset!r}"
    assert (type(args.tasks) is int and args.tasks >= 1 and args.total_items % args.tasks == 0
            and args.total_items // args.tasks >= 2), \
        f"total_items / tasks: expected an integer >= 2, measured {args.total_items} / {args.tasks}"
    return args
