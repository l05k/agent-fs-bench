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

Throughput is ``n*N / max-task phase time`` from mdtest's aggregate CSV row
(including the end barrier); create/read with S>0 also report
``n*N*S / time`` in MiB/s. Reads follow writes in the same launch without
cache drops, so they may be served by the client page cache.

``tasks = None`` fills the selected machines at two tasks per logical CPU
(``oversubscribe = 2``); oversubscribed points are intended. A peak curve is several runs
with explicit ``tasks`` (for example powers of two up to 2x CPUs) pooled with
``plot --vary tasks``.
"""

from __future__ import annotations

import argparse
from typing import Any, Sequence

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.benchmarks.mdtest import (
    MDTEST_VERSION, BenchmarkError, check_banner, check_workload, launch, read_phases,
    unexpected_warnings,
)
from agent_fs_bench.results import RunContext

CACHE_STATE = "write-then-read-same-launch-no-drop"
COMPLETION = "fsync-per-file-after-write"
MIB = 1024 * 1024

CONFIG: dict[str, Any] = {
    "mdtest_bin": "mdtest",
    "tasks": None,
    "cpus_per_task": 1,
    "oversubscribe": 2,
    # One file size per run.
    "file_size_bytes": 4096,
    # None: files per task from ITEMS_PER_TASK for this size; an int overrides it.
    "items_per_task": None,
    "repeats": 3,
    "layout": "private",
}

# Larger files get fewer items per task, so the 2x-CPU default stays within a few tens
# of GiB per launch (128 tasks × 256 × 1 MiB = 32 GiB).
ITEMS_PER_TASK = {0: 8192, 4096: 8192, 65536: 2048, 1048576: 256}

# mdtest's "File read" column is a real data read here; rename its phase.
OPERATION_NAMES = {"file-create": "file-create", "file-stat": "file-stat",
                   "file-open": "file-read", "file-unlink": "file-unlink"}
DATA_OPERATIONS = ("file-create", "file-read")

FIGURES = [
    Figure('phases', 'tasks', 'ops_per_s', split=('operation', 'layout', 'file_size_bytes'),
           xlabel='Tasks', ylabel='Throughput (Kops/s)', yscale=1000, logx=True),
    Figure('phases', 'tasks', 'mib_per_s', split=('operation', 'layout', 'file_size_bytes'),
           xlabel='Tasks', ylabel='Bandwidth (MiB/s)', logx=True),
]


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    slots = cluster.allocate(ctx.pool.nodes, args.tasks, args.cpus_per_task, args.oversubscribe)
    tasks, size, items = len(slots), args.file_size_bytes, args.items_per_task
    total = items * tasks
    options = ["-a", "POSIX", "-F", "-n", str(items), "-i", "1", "-w", str(size), "-e", str(size),
               "-y"]
    ctx.info.update(mdtest_version=MDTEST_VERSION, cache_state=CACHE_STATE, completion=COMPLETION)
    for repeat in range(1, args.repeats + 1):
        name = f"S{size}-{args.layout}-N{tasks}-rep{repeat}"
        print(f"  {name}: {items} files/task of {size} B", flush=True)
        stdout, text = launch(ctx, slots, args.mdtest_bin, options, args.layout, name)
        check_banner(stdout, tasks, name)
        check_workload(stdout, "file", tasks, total, name)
        warnings = unexpected_warnings(stdout)
        if warnings:
            raise BenchmarkError(f"{name}: unexpected mdtest warnings: {'; '.join(warnings)}")
        summary = []
        for phase, ops, elapsed, rate in read_phases(text, "file", total):
            operation = OPERATION_NAMES[phase]
            mib_per_s = (ops * size / MIB / elapsed
                         if size and operation in DATA_OPERATIONS else None)
            ctx.emit(
                "phases",
                {"operation": operation, "layout": args.layout, "tasks": tasks, "repeat": repeat,
                 "file_size_bytes": size, "items_per_task": items},
                {"ops": ops, "elapsed_s": elapsed, "ops_per_s": rate, "mib_per_s": mib_per_s},
            )
            summary.append(f"{operation} {rate:.0f} ops/s"
                           + (f" {mib_per_s:.1f} MiB/s" if mib_per_s is not None else ""))
        print(f"  {name}: " + ", ".join(summary), flush=True)


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
        if args.file_size_bytes not in ITEMS_PER_TASK:
            raise BenchmarkError(f"no default items_per_task for {args.file_size_bytes} B; "
                                 f"set it explicitly (defaults: {ITEMS_PER_TASK})")
        args.items_per_task = ITEMS_PER_TASK[args.file_size_bytes]
    if (args.repeats < 1 or args.layout not in ("private", "shared")
            or type(args.items_per_task) is not int or args.items_per_task < 1
            or type(args.file_size_bytes) is not int or args.file_size_bytes < 0):
        raise BenchmarkError("layout must be private or shared, with positive repeats and "
                             "items_per_task and a non-negative file_size_bytes")
    return args
