"""Fixed-work metadata operations in IOR's mdtest, one process count per launch.

Each case runs one mdtest (IOR 4.0.0) process launch through the configured MPI
launcher: ``-a POSIX -n total_items/P -i 1`` plus ``-F`` (files) or ``-D``
(directories), with ``-u`` for private per-process working directories and
without it for shared ones. ``-n`` is mdtest's per-process item count, so every
case keeps the same ``total_items`` aggregate work. No file data is written or
read: mdtest's read_bytes keeps its default 0, so its "File read" phase only
opens and closes each file.

mdtest times each phase between barriers. The CSV's aggregate ``all`` row
provides max(time) over ranks; throughput is the fixed total_items / max(time).
IOR 4.0.0 overwrites aggregate directory create/stat item counts with rank 0's
local counts, so CSV rate/item fields are not used. Per-rank timings are not
used either. Completion is an operation returning, without extra sync or cache
drops between phases.

Before a case emits anything, mdtest's launch banner must report the pinned
version, exactly ``P`` total tasks and one node. mdtest's known zero-byte read
notice is expected and any other warning fails the case; a nonzero exit fails
immediately, with no retry. stdout/stderr stay in ``ctx.result_dir`` and the CSV
is deleted once parsed, leaving JSONL as the only measurement record. Each case
gets a fresh directory under ``ctx.work_dir``, removed after success and kept
after a failure for diagnosis.

Only --smoke is exposed on the workload CLI; edit CONFIG for a deliberate
experiment. Targets are mounted externally.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Sequence

from agent_fs_bench.results import RunContext

MDTEST_VERSION = "4.0.0"
CACHE_STATE = "create-then-access-no-drop"
COMPLETION = "operation-return"

# mdtest_bin and mpi_command are resolved through PATH. mpi_command is the
# launcher followed by its process-count flag.
CONFIG: dict[str, Any] = {
    "mdtest_bin": "mdtest",
    "mpi_command": ("mpiexec", "-n"),
    "total_items": 65536,
    "conc_levels": (1, 2, 4, 8, 16),
    "repeats": 5,
    "layouts": ("private", "shared"),
    "subsets": ("file", "dir"),
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

class BenchmarkError(RuntimeError):
    pass

@dataclass(frozen=True)
class Case:
    subset: str
    layout: str
    concurrency: int
    repeat: int

    @property
    def name(self) -> str:
        return f"{self.subset}-{self.layout}-P{self.concurrency}-rep{self.repeat}"

def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

def mdtest_command(
    args: argparse.Namespace, case: Case, items: int,
    directory: Path, csv_path: Path,
) -> list[str]:
    """One mdtest launch: fixed aggregate work, one iteration, no extra phases."""
    command = [
        *args.mpi_command, str(case.concurrency),
        args.mdtest_bin, "-a", "POSIX", "-n", str(items), "-i", "1",
    ]
    if case.layout == "private":
        command.append("-u")
    command.append("-F" if case.subset == "file" else "-D")
    command += [
        "-d", str(directory),
        f"--saveRankPerformanceDetails={csv_path}",
    ]
    return command

def check_banner(text: str, concurrency: int, log_path: Path) -> None:
    """Require mdtest's own report of version, task count and node count."""
    matches = BANNER.findall(text)
    if len(matches) != 1:
        raise BenchmarkError(
            f"{log_path}: expected one mdtest launch banner, found {len(matches)}"
        )
    version, tasks, nodes = matches[0]
    if version != MDTEST_VERSION:
        raise BenchmarkError(
            f"{log_path}: mdtest reported version {version}, expected {MDTEST_VERSION}"
        )
    if int(tasks) != concurrency or int(nodes) != 1:
        raise BenchmarkError(
            f"{log_path}: mdtest launched {tasks} task(s) on {nodes} node(s), "
            f"expected {concurrency} task(s) on 1 node"
        )

def check_workload(text: str, case: Case, total_items: int, log_path: Path) -> None:
    """Require the reported process count, total work and object type to match."""
    workload = re.findall(r"^(\d+) tasks, (\d+) (files|directories)$", text, re.MULTILINE)
    expected = (str(case.concurrency), str(total_items),
                "files" if case.subset == "file" else "directories")
    if workload != [expected]:
        raise BenchmarkError(f"{case.name}: mdtest did not report the expected fixed work; log: {log_path}")

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

def read_phases(
    csv_path: Path, subset: str, expected_ops: int,
) -> list[tuple[str, int, float, float]]:
    """Use aggregate phase times, not IOR 4.0.0's corrupted CSV item counts.

    Every successful phase operates on the fixed total work. Per-rank rows and
    the non-operation Directory read / Tree columns are not measurements here.
    """
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise BenchmarkError(f"{csv_path}: empty mdtest CSV")
    columns = {name: position for position, name in enumerate(rows[0])}
    if rows[0][:2] != ["rank", "items"] or len(columns) != len(rows[0]):
        raise BenchmarkError(f"{csv_path}: unexpected mdtest CSV header")
    aggregates = [row for row in rows[1:] if row[:1] == ["all"]]
    if len(aggregates) != 1:
        raise BenchmarkError(
            f"{csv_path}: expected exactly one aggregate 'all' row, found {len(aggregates)}"
        )
    row = aggregates[0]
    if len(row) < len(rows[0]):
        raise BenchmarkError(f"{csv_path}: aggregate 'all' row is incomplete")
    phases = []
    for operation, column, phase_subset in PHASES:
        if phase_subset != subset:
            continue
        time_column = f"time-{column}"
        if time_column not in columns:
            raise BenchmarkError(f"{csv_path}: no {time_column} column")
        time_text = row[columns[time_column]]
        if not time_text:
            raise BenchmarkError(f"{csv_path}: {column} phase did not run")
        elapsed = float(time_text)
        if not math.isfinite(elapsed) or elapsed <= 0:
            raise BenchmarkError(f"{csv_path}: {column} has no usable time: {time_text}")
        phases.append((operation, expected_ops, elapsed, expected_ops / elapsed))
    return phases

def build_plan(args: argparse.Namespace) -> list[Case]:
    return [
        Case(subset, layout, concurrency, repeat)
        for subset in args.subsets
        for layout in args.layouts
        for concurrency in args.conc_levels
        for repeat in range(1, args.repeats + 1)
    ]

def run_case(ctx: RunContext, args: argparse.Namespace, case: Case) -> None:
    items, remainder = divmod(args.total_items, case.concurrency)
    if remainder or items < 1:
        raise BenchmarkError(
            f"{case.name}: total_items {args.total_items} must be divisible "
            f"by concurrency {case.concurrency}"
        )
    directory = ctx.work_dir / case.name
    log_path = ctx.result_dir / f"{case.name}.log"
    csv_path = ctx.result_dir / f"{case.name}.csv"
    directory.mkdir()
    command = mdtest_command(args, case, items, directory, csv_path)
    log(f"[{ctx.target.name}] {case.name}: {items} items/process")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as error:
        raise BenchmarkError(
            f"{case.name}: cannot launch {shlex.join(command)}: {error}"
        ) from error
    log_path.write_text(
        f"$ {shlex.join(command)}\n{completed.stdout}{completed.stderr}",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise BenchmarkError(
            f"{case.name}: mdtest exited with status {completed.returncode}; log: {log_path}"
        )
    check_banner(completed.stdout, case.concurrency, log_path)
    check_workload(completed.stdout, case, args.total_items, log_path)
    warnings = unexpected_warnings(completed.stdout)
    if warnings:
        raise BenchmarkError(
            f"{case.name}: unexpected mdtest warnings: {'; '.join(warnings)}; log: {log_path}"
        )
    phases = read_phases(csv_path, case.subset, args.total_items)
    for operation, ops, elapsed, rate in phases:
        ctx.emit(
            f"{operation}/{case.layout}/N{args.total_items}/P{case.concurrency}",
            {
                "concurrency": case.concurrency,
                "ops": ops,
                "elapsed_s": elapsed,
                "ops_per_s": rate,
            },
            repeat=case.repeat,
            details={"operation": operation, "layout": case.layout},
        )
    log(f"[{ctx.target.name}] {case.name}: " + ", ".join(
        f"{operation} {rate:.1f} ops/s" for operation, _ops, _elapsed, rate in phases
    ))
    shutil.rmtree(directory)
    csv_path.unlink()

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    args.mdtest_bin = shutil.which(args.mdtest_bin) or ""
    if not args.mdtest_bin:
        raise BenchmarkError(f"mdtest executable not found: {CONFIG['mdtest_bin']}")
    args.mpi_command = (shutil.which(args.mpi_command[0]) or "", *args.mpi_command[1:])
    if not args.mpi_command[0]:
        raise BenchmarkError(f"MPI launcher not found: {CONFIG['mpi_command'][0]}")
    with Path(args.mdtest_bin).open('rb') as binary:
        executable_sha256 = hashlib.file_digest(binary, 'sha256').hexdigest()
    ctx.parameters.clear()
    ctx.parameters.update(
        total_items=args.total_items,
        conc_levels=list(args.conc_levels),
        layouts=list(args.layouts),
        subsets=list(args.subsets),
        repeats=args.repeats,
        smoke=args.smoke,
        mdtest_version=MDTEST_VERSION,
        cache_state=CACHE_STATE,
        completion=COMPLETION,
        mdtest_bin=args.mdtest_bin,
        mpi_command=list(args.mpi_command),
        executable_sha256=executable_sha256,
    )
    for case in build_plan(args):
        run_case(ctx, args, case)

def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="run a small real sweep with two repeats")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    if parsed.smoke:
        settings.update(total_items=128, conc_levels=(1, 2), repeats=2)
    args = argparse.Namespace(**settings, **vars(parsed))
    if args.repeats < 1 or not args.conc_levels:
        raise BenchmarkError("repeats and process counts must be positive")
    for processes in args.conc_levels:
        if processes < 1 or args.total_items % processes or args.total_items // processes < 2:
            raise BenchmarkError("total_items must divide evenly into at least two items per process")
    if not args.layouts or set(args.layouts) - {"private", "shared"}:
        raise BenchmarkError("layouts must select private and/or shared")
    if not args.subsets or set(args.subsets) - {"file", "dir"}:
        raise BenchmarkError("subsets must select file and/or dir")
    return args
