"""Single-client mdtest peak: weak-scaling file sweep up to 2x the client CPUs.

Each case is one IOR 4.0.0 mdtest launch with ``P`` processes and a fixed
per-process item count ``n``, so total work is ``n*P`` and phase time stays
long enough at high P to show the saturation plateau. Every launch runs
``-F -a POSIX -i 1 -n n -w S -e S -y`` for one file size ``S``:

- file-create: open(O_CREAT), write S bytes, fsync, close (``-y`` only
  fsyncs after a write, so S=0 is create+close without fsync)
- file-stat:   stat
- file-read:   open, read S bytes, close (S=0 is open/close only)
- file-unlink: unlink

Throughput is ``n*P / max-rank phase time`` from mdtest's aggregate CSV row
(including the end barrier); create/read with S>0 also report
``n*P*S / time`` in MiB/s. Reads follow writes in the same launch without
cache drops, so they may be served by the client page cache.

The process sweep is powers of two up to 2x the available logical CPUs, plus
the CPU count itself; oversubscribed points are intended. ``mpi_command``
must therefore allow oversubscription (Open MPI ``--oversubscribe``).
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Sequence

from agent_fs_bench.benchmarks.mdtest import (
    MDTEST_VERSION, BenchmarkError, Case, check_banner, check_workload, log,
    read_phases, unexpected_warnings,
)
from agent_fs_bench.results import RunContext

CACHE_STATE = "write-then-read-same-launch-no-drop"
COMPLETION = "fsync-per-file-after-write"
MIB = 1024 * 1024

CONFIG: dict[str, Any] = {
    "mdtest_bin": "mdtest",
    "mpi_command": ("mpiexec", "--oversubscribe", "-n"),
    # File size in bytes -> files per process. Larger files get fewer items so
    # the 2x-CPU point stays within a few tens of GiB per launch.
    "items_per_proc": {0: 8192, 4096: 8192, 65536: 2048, 1048576: 256},
    # None: powers of two up to 2x available logical CPUs, plus the CPU count.
    "conc_levels": None,
    "repeats": 3,
    "layouts": ("private", "shared"),
}

# mdtest's "File read" column is a real data read here; rename its phase.
OPERATION_NAMES = {"file-create": "file-create", "file-stat": "file-stat",
                   "file-open": "file-read", "file-unlink": "file-unlink"}
DATA_OPERATIONS = ("file-create", "file-read")


def available_cpus() -> int:
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()


def default_conc_levels(cpus: int) -> tuple[int, ...]:
    levels = {cpus, 2 * cpus}
    level = 1
    while level <= 2 * cpus:
        levels.add(level)
        level *= 2
    return tuple(sorted(levels))


def mdtest_command(args: argparse.Namespace, layout: str, concurrency: int,
                   size: int, items: int, directory: Path, csv_path: Path) -> list[str]:
    command = [
        *args.mpi_command, str(concurrency),
        args.mdtest_bin, "-a", "POSIX", "-F", "-n", str(items), "-i", "1",
        "-w", str(size), "-e", str(size), "-y",
    ]
    if layout == "private":
        command.append("-u")
    return command + ["-d", str(directory), f"--saveRankPerformanceDetails={csv_path}"]


def run_case(ctx: RunContext, args: argparse.Namespace, layout: str,
             concurrency: int, size: int, repeat: int) -> None:
    items = args.items_per_proc[size]
    total = items * concurrency
    name = f"S{size}-{layout}-P{concurrency}-rep{repeat}"
    directory = ctx.work_dir / name
    log_path = ctx.result_dir / f"{name}.log"
    csv_path = ctx.result_dir / f"{name}.csv"
    directory.mkdir()
    command = mdtest_command(args, layout, concurrency, size, items, directory, csv_path)
    log(f"[{ctx.target.name}] {name}: {items} files/process of {size} B")
    completed = subprocess.run(command, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    log_path.write_text(f"$ {shlex.join(command)}\n{completed.stdout}{completed.stderr}",
                        encoding="utf-8")
    if completed.returncode != 0:
        raise BenchmarkError(f"{name}: mdtest exited with status {completed.returncode}; log: {log_path}")
    check_banner(completed.stdout, concurrency, log_path)
    check_workload(completed.stdout, Case("file", layout, concurrency, repeat), total, log_path)
    warnings = unexpected_warnings(completed.stdout)
    if warnings:
        raise BenchmarkError(f"{name}: unexpected mdtest warnings: {'; '.join(warnings)}; log: {log_path}")
    summary = []
    for phase, ops, elapsed, rate in read_phases(csv_path, "file", total):
        operation = OPERATION_NAMES[phase]
        metrics = {"concurrency": concurrency, "ops": ops, "elapsed_s": elapsed, "ops_per_s": rate}
        if size and operation in DATA_OPERATIONS:
            metrics["bytes"] = ops * size
            metrics["mib_per_s"] = ops * size / MIB / elapsed
        ctx.emit(
            f"{operation}/{layout}/S{size}/n{items}/P{concurrency}", metrics, repeat=repeat,
            details={"operation": operation, "layout": layout,
                     "file_size_bytes": size, "items_per_proc": items},
        )
        summary.append(f"{operation} {rate:.0f} ops/s"
                       + (f" {metrics['mib_per_s']:.1f} MiB/s" if "mib_per_s" in metrics else ""))
    log(f"[{ctx.target.name}] {name}: " + ", ".join(summary))
    shutil.rmtree(directory)
    csv_path.unlink()


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    args.mdtest_bin = shutil.which(args.mdtest_bin) or ""
    if not args.mdtest_bin:
        raise BenchmarkError(f"mdtest executable not found: {CONFIG['mdtest_bin']}")
    args.mpi_command = (shutil.which(args.mpi_command[0]) or "", *args.mpi_command[1:])
    if not args.mpi_command[0]:
        raise BenchmarkError(f"MPI launcher not found: {CONFIG['mpi_command'][0]}")
    with Path(args.mdtest_bin).open("rb") as binary:
        executable_sha256 = hashlib.file_digest(binary, "sha256").hexdigest()
    ctx.parameters.clear()
    ctx.parameters.update(
        items_per_proc={str(size): items for size, items in args.items_per_proc.items()},
        conc_levels=list(args.conc_levels),
        client_cpus=args.client_cpus,
        layouts=list(args.layouts),
        repeats=args.repeats,
        smoke=args.smoke,
        mdtest_version=MDTEST_VERSION,
        cache_state=CACHE_STATE,
        completion=COMPLETION,
        mdtest_bin=args.mdtest_bin,
        mpi_command=list(args.mpi_command),
        executable_sha256=executable_sha256,
    )
    for size in args.items_per_proc:
        for layout in args.layouts:
            for concurrency in args.conc_levels:
                for repeat in range(1, args.repeats + 1):
                    run_case(ctx, args, layout, concurrency, size, repeat)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smoke", action="store_true",
                        help="tiny real sweep: 16 files/process, P=1,2, one repeat")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    settings["client_cpus"] = available_cpus()
    if settings["conc_levels"] is None:
        settings["conc_levels"] = default_conc_levels(settings["client_cpus"])
    if parsed.smoke:
        settings.update(items_per_proc={size: 16 for size in CONFIG["items_per_proc"]},
                        conc_levels=(1, 2), repeats=1)
    args = argparse.Namespace(**settings, **vars(parsed))
    if args.repeats < 1 or not args.conc_levels or min(args.conc_levels) < 1:
        raise BenchmarkError("repeats and process counts must be positive")
    if not args.items_per_proc or min(args.items_per_proc.values()) < 2 or min(args.items_per_proc) < 0:
        raise BenchmarkError("items_per_proc needs sizes >= 0 with at least two files per process")
    if not args.layouts or set(args.layouts) - {"private", "shared"}:
        raise BenchmarkError("layouts must select private and/or shared")
    return args
