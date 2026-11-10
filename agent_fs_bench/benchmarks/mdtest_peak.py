"""mdtest peak: weak-scaling file phases at one task count, up to 2x the CPUs.

Each repeat is one IOR 4.0.0 mdtest job with ``N`` tasks and a fixed per-task item count
``n``, so total work is ``n*N`` and phase time stays long enough at high N to show the
saturation plateau. Every launch runs ``-F -a POSIX -i 1 -n n -w S -e S -y`` for one file
size ``S``:

- file-create: open(O_CREAT), write S bytes, fsync, close (``-y`` only
  fsyncs after a write, so S=0 is create+close without fsync)
- file-stat:   stat
- file-read:   open, read S bytes, close (S=0 is open/close only)
- file-unlink: unlink

| Symbol | Definition |
| --- | --- |
| N, n, S | `tasks`, `items_per_task`, `file_size_bytes` |
| T_p | Aggregate phase time (maximum task time including the end barrier) |
| X_p | `ops_per_s = n*N / T_p` (table `phases`) |
| B_p | `mib_per_s = n*N*S / T_p / 2^20` for create/read with S > 0 (table `bandwidth`) |
| t_{p,r} | Rank r's phase time before the end barrier (table `ranks`) |

Reads follow writes in the same launch without cache drops, so they may be served by the
client page cache. There is no warm-up: every repeat is measured. Tasks above the CPU
count oversubscribe (round-robin per machine); a peak curve is several runs with
different ``tasks`` (for example powers of two up to 2x CPUs) pooled with
``plot --vary tasks``.
"""

from __future__ import annotations

import argparse
from typing import Any, Sequence

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.benchmarks.mdtest import (
    MDTEST_VERSION, binary_info, launch, read_phases, read_rank_times, validate, write_repeat,
)
from agent_fs_bench.results import RunContext

CACHE_STATE = "write-then-read-same-launch-no-drop"
COMPLETION = "fsync-per-file-after-write"
MIB = 1024 * 1024

CONFIG: dict[str, Any] = {
    "mdtest_bin": "mdtest",
    "tasks": 128,
    # One file size per run.
    "file_size_bytes": 4096,
    # None: files per task from ITEMS_PER_TASK for this size; an int overrides it.
    "items_per_task": None,
    "repeats": 3,
    "layout": "private",
}

# Larger files get fewer items per task, so the default 128 tasks stay within a few tens
# of GiB per launch (128 tasks × 256 × 1 MiB = 32 GiB).
ITEMS_PER_TASK = {0: 8192, 4096: 8192, 65536: 2048, 1048576: 256}

# mdtest's "File read" column is a real data read here; rename its phase.
OPERATION_NAMES = {"file-create": "file-create", "file-stat": "file-stat",
                   "file-open": "file-read", "file-unlink": "file-unlink"}
DATA_OPERATIONS = ("file-create", "file-read")

FIGURES = [
    Figure('phases', 'tasks', 'ops_per_s', split=('operation', 'layout', 'file_size_bytes'),
           xlabel='Tasks', ylabel='Throughput (Kops/s)', yscale=1000, logx=True),
    Figure('bandwidth', 'tasks', 'mib_per_s', split=('operation', 'layout', 'file_size_bytes'),
           xlabel='Tasks', ylabel='Bandwidth (MiB/s)', logx=True),
]


def bandwidth(total_items: int, size: int, elapsed_s: float) -> float:
    """B_p = n*N*S / T_p in MiB/s."""
    return total_items * size / MIB / elapsed_s


def write_bandwidth(ctx: RunContext, keys: dict[str, Any], size: int,
                    phases: list[tuple[str, int, float, float]]) -> None:
    for operation, ops, elapsed, _rate in phases:
        if size and operation in DATA_OPERATIONS:
            ctx.emit("bandwidth", {"operation": operation, **keys},
                     {"bytes": ops * size, "elapsed_s": elapsed,
                      "mib_per_s": bandwidth(ops, size, elapsed)})


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    placement = cluster.place(ctx.pool.nodes, args.tasks)
    tasks, size, items = len(placement), args.file_size_bytes, args.items_per_task
    total = items * tasks
    options = ["-a", "POSIX", "-F", "-n", str(items), "-i", "1", "-w", str(size), "-e", str(size),
               "-y"]
    ctx.info.update(mdtest_version=MDTEST_VERSION, cache_state=CACHE_STATE, completion=COMPLETION,
                    **binary_info(ctx.pool.launcher, placement, args.mdtest_bin))
    for repeat in range(1, args.repeats + 1):
        name = f"S{size}-{args.layout}-N{tasks}-rep{repeat}"
        print(f"  {name}: {items} files/task of {size} B", flush=True)
        stdout, text = launch(ctx, placement, args.mdtest_bin, options, args.layout, name)
        validate(stdout, "file", tasks, total, name)
        phases = [(OPERATION_NAMES[operation], ops, elapsed, rate)
                  for operation, ops, elapsed, rate in read_phases(text, "file", total)]
        ranks = {OPERATION_NAMES[operation]: times
                 for operation, times in read_rank_times(text, "file", tasks).items()}
        keys = {"layout": args.layout, "tasks": tasks, "repeat": repeat,
                "file_size_bytes": size, "items_per_task": items}
        write_repeat(ctx, keys, placement, phases, ranks)
        write_bandwidth(ctx, keys, size, phases)
        print(f"  {name}: " + ", ".join(f"{operation} {rate:.0f} ops/s"
                                        for operation, _ops, _elapsed, rate in phases), flush=True)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smoke", action="store_true",
                        help="tiny real run: 16 files/task, one repeat")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    if parsed.smoke:
        settings.update(items_per_task=16, repeats=1)
    args = argparse.Namespace(**settings, **vars(parsed))
    if args.items_per_task is None:
        assert args.file_size_bytes in ITEMS_PER_TASK, \
            f"default items_per_task: expected a size in {ITEMS_PER_TASK}, " \
            f"measured {args.file_size_bytes} B; set items_per_task explicitly"
        args.items_per_task = ITEMS_PER_TASK[args.file_size_bytes]
    assert args.repeats >= 1, f"repeats: expected >= 1, measured {args.repeats}"
    assert args.layout in ("private", "shared"), \
        f"layout: expected private or shared, measured {args.layout!r}"
    assert type(args.items_per_task) is int and args.items_per_task >= 1, \
        f"items_per_task: expected positive integer, measured {args.items_per_task!r}"
    assert type(args.file_size_bytes) is int and args.file_size_bytes >= 0, \
        f"file_size_bytes: expected non-negative integer, measured {args.file_size_bytes!r}"
    return args
