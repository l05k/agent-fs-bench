#!/usr/bin/env python3
"""Run SQLite's official single-writer BLOB overwrite microbenchmark.

The harness builds SQLite 3.54.1's ``test/kvtest.c`` with a small checked patch
that surfaces statement and commit errors, reads back the effective settings,
and counts completed operations. It runs random overwrites, then a standalone
read-only invocation of the same database. ``multitrans=True`` commits each
overwrite; False batches the entire run. The default NAS baseline uses DELETE
journaling and FULL synchronization. It is not an OpenClaw transcript replay or
an application-latency estimate.

OpenClaw's per-agent SQLite store motivates studying transactional storage.
The reference revision is 5f6f15fed0d6ba618cf781c9686a8b93f499564b; see
``docs/bench-kvtest-openclaw.md`` for source links and a calibration plan.
Static analysis establishes schema and configured persistence policy, not
event-size frequencies, statements per transaction, or transactions per turn.
CONFIG sizes and row counts are synthetic controls, not measured OpenClaw data.

Important differences: kvtest overwrites existing rows in one table, omits
transcript metadata/FTS/compression, and offers FULL synchronization or
``--nosync`` (OFF), not the reference agent store's NORMAL setting. Agent
parameters are not replayed. Keep SQLite settings fixed across filesystems.
Never run concurrent writers/readers against the measured database: even with
checked statements and commits, integrity_check is not proof of crash
durability. A task owns its database exclusively.

A run starts ``tasks`` independent kvtest instances together, one per task
slot. Every task initializes and measures its own database under the run's
per-task directory, so no database ever has concurrent users. Repeats of all
instances are aligned by MPI barriers. All timed write repeats finish first;
a standalone read-only phase then repeats its own invocations against the same
database. Each repeat emits one write or read row per task. Pool runs with
different ``tasks`` values for a concurrency curve.

Each timed write invocation emits one row: the experiment settings, ``tasks``,
repeat index, task ``sample`` and node ``host`` as key columns, then
``microseconds_per_write``, ``write_rate_mb_s`` and ``total_elapsed_s``. A
separate read-only invocation of the same database emits the corresponding
``reads`` row. Reported microseconds amortize connection setup and final
close/checkpoint; they are not individually sampled transaction latencies. The
observed journal mode, cache size, synchronous setting and page size, the
checked completed/committed counts, the build provenance and the final
integrity-check result go to the run manifest. The node leader on each machine
downloads or verifies the pinned sources, applies the checked patch and builds
the binary once per machine, before any task touches the target. A machine
without network access needs the sources pre-placed in its build directory.

The workload itself uses the standard library; task dispatch uses the shared MPI
framework. A C compiler and network access are needed for the first source
download/build on each machine.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import RunContext

from . import sqlite_tools

DB_FILENAME = "kvtest.sqlite"

DEFAULT_PAYLOAD = 2048
DEFAULT_VARIANCE = 0
DEFAULT_COUNT = 2000
DEFAULT_CACHE_KIB = 1000

# Deliberate experiment settings. Edit for an ablation; they become the
# per-row key columns. build_dir, force_download and the task controls are
# run management. Private settings must never be committed.
CONFIG = {
    "size": DEFAULT_PAYLOAD,
    "variance": DEFAULT_VARIANCE,
    "count": DEFAULT_COUNT,
    "cache_size": DEFAULT_CACHE_KIB,
    "jmode": "delete",
    "nosync": False,
    "mmap": 0,
    "multitrans": True,
    "repeats": 5,
    "tasks": 1,
    "build_dir": None,
    "force_download": False,
}

FIGURES = [
    Figure(
        "repeats", "tasks", "microseconds_per_write", kind="box",
        split=("transaction",), xlabel="Tasks", ylabel="Microseconds per write",
        name="kvtest-microseconds-per-write",
    ),
    Figure(
        "reads", "tasks", "microseconds_per_read", kind="box",
        split=("transaction",), xlabel="Tasks", ylabel="Microseconds per read",
        name="kvtest-microseconds-per-read",
    ),
]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run SQLite's official single-writer kvtest against the filesystem under test."
        )
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="two tasks, two repeats, 200 writes then 200 reads; needs two logical CPUs",
    )
    options = parser.parse_args(argv)
    values = dict(CONFIG)
    if options.smoke:
        values.update(tasks=2, repeats=2, count=200)
    args = argparse.Namespace(**values, smoke=options.smoke)
    args.jmode = args.jmode.lower()
    return args


COMMON_PATTERNS = {
    "sqlite_version": re.compile(
        r"^SQLite version:[ \t]*(\S+)[ \t]*\r?$", re.MULTILINE
    ),
    "settings": re.compile(
        r"^--cache-size[ \t]+(-?\d+)[ \t]+--jmode[ \t]+(\w+)"
        r"[ \t]+--synchronous[ \t]+(\d+)[ \t]*\r?$",
        re.MULTILINE,
    ),
    "page_size": re.compile(
        r"^Database page size:[ \t]*(\d+)[ \t]*\r?$", re.MULTILINE
    ),
    "total_elapsed_s": re.compile(
        r"^Total elapsed time:[ \t]*([0-9]+\.[0-9]+)[ \t]*\r?$",
        re.MULTILINE,
    ),
}
WRITE_PATTERNS = {
    "microseconds_per_write": re.compile(
        r"^Microseconds per BLOB write:[ \t]*([0-9]+\.[0-9]+)[ \t]*\r?$",
        re.MULTILINE,
    ),
    "write_rate_mb_s": re.compile(
        r"^Content write rate:[ \t]*([0-9]+\.[0-9]+)[ \t]*MB/s[ \t]*\r?$",
        re.MULTILINE,
    ),
    "completed_updates": re.compile(
        r"^Completed updates:[ \t]*(\d+) of (\d+)[ \t]*\r?$", re.MULTILINE
    ),
    "committed_transactions": re.compile(
        r"^Committed transactions:[ \t]*(\d+)[ \t]*\r?$", re.MULTILINE
    ),
}
READ_PATTERNS = {
    "microseconds_per_read": re.compile(
        r"^Microseconds per BLOB read:[ \t]*([0-9]+\.[0-9]+)[ \t]*\r?$",
        re.MULTILINE,
    ),
    "read_rate_mb_s": re.compile(
        r"^Content read rate:[ \t]*([0-9]+\.[0-9]+)[ \t]*MB/s[ \t]*\r?$",
        re.MULTILINE,
    ),
    "completed_reads": re.compile(
        r"^Completed reads:[ \t]*(\d+) of (\d+)[ \t]*\r?$", re.MULTILINE
    ),
}
INTEGRITY_PATTERN = re.compile(
    r"^Integrity-check:[ \t]*(\S.*?)[ \t]*\r?$", re.MULTILINE
)


def parse_kvtest_output(text: str, *, update: bool) -> Dict[str, Any]:
    """Parse one ``kvtest run`` invocation, rejecting missing measured fields.

    Only values kvtest itself prints are returned. The checked patch reads back
    the effective journal mode, cache size, synchronous setting and page size,
    and counts completed statements and commits. ``--mmap`` is merely echoed,
    so it is not reported as observed.
    """
    patterns = {**COMMON_PATTERNS, **(WRITE_PATTERNS if update else READ_PATTERNS)}
    matches: Dict[str, re.Match] = {}
    missing: List[str] = []
    for name, pattern in patterns.items():
        match = pattern.search(text)
        if match is None:
            missing.append(name)
        else:
            matches[name] = match
    if missing:
        raise sqlite_tools.BenchmarkError("could not parse kvtest output; missing "
        + ", ".join(missing)
        + ". Full output:\n"
        + text)
    parsed = {
        "sqlite_version": matches["sqlite_version"].group(1),
        "journal_mode": matches["settings"].group(2),
        "cache_size": int(matches["settings"].group(1)),
        "synchronous": int(matches["settings"].group(3)),
        "page_size": int(matches["page_size"].group(1)),
        "total_elapsed_s": float(matches["total_elapsed_s"].group(1)),
    }
    if update:
        parsed["completed_updates"] = int(matches["completed_updates"].group(1))
        parsed["attempted_updates"] = int(matches["completed_updates"].group(2))
        parsed["committed_transactions"] = int(
            matches["committed_transactions"].group(1)
        )
        parsed["microseconds_per_write"] = float(
            matches["microseconds_per_write"].group(1)
        )
        parsed["write_rate_mb_s"] = float(matches["write_rate_mb_s"].group(1))
    else:
        parsed["completed_reads"] = int(matches["completed_reads"].group(1))
        parsed["attempted_reads"] = int(matches["completed_reads"].group(2))
        parsed["microseconds_per_read"] = float(
            matches["microseconds_per_read"].group(1)
        )
        parsed["read_rate_mb_s"] = float(matches["read_rate_mb_s"].group(1))
    return parsed


def verify_output(
    parsed: Dict[str, Any],
    args: argparse.Namespace,
    *,
    update: bool,
    where: str,
) -> None:
    """Require effective settings and completed/committed counts to match."""
    expected = {
        "journal_mode": args.jmode,
        "cache_size": -args.cache_size,
        "synchronous": 0 if args.nosync else 2,
    }
    if update:
        expected.update(
            completed_updates=args.count,
            attempted_updates=args.count,
            committed_transactions=args.count if args.multitrans else 1,
        )
    else:
        expected.update(completed_reads=args.count, attempted_reads=args.count)
    for name, wanted in expected.items():
        measured = parsed[name]
        if measured != wanted:
            raise sqlite_tools.BenchmarkError(
                f"{where}: {name}: expected {wanted}, measured {measured}"
            )


def parse_integrity_check(text: str) -> str:
    """Parse ``kvtest stat`` output; every printed check must be ``ok``."""
    values = INTEGRITY_PATTERN.findall(text)
    if not values:
        raise sqlite_tools.BenchmarkError("could not parse kvtest stat output; missing "
        "'Integrity-check'. Full output:\n" + text)
    failed = [value for value in values if value != "ok"]
    if failed:
        raise sqlite_tools.BenchmarkError("kvtest integrity-check failed: "
        + "; ".join(failed)
        + "\nFull output:\n"
        + text)
    return "ok"


def init_argv(binary: Path, db_path: Path, args: argparse.Namespace) -> List[str]:
    return [
        str(binary),
        "init",
        str(db_path),
        "--count",
        str(args.count),
        "--size",
        str(args.size),
        "--variance",
        str(args.variance),
    ]


def run_argv(
    binary: Path,
    db_path: Path,
    args: argparse.Namespace,
    *,
    update: bool,
) -> List[str]:
    argv = [
        str(binary),
        "run",
        str(db_path),
        "--count",
        str(args.count),
        "--random",
        "--jmode",
        args.jmode,
        # Negative cache_size means KiB; kvtest reads the value back.
        "--cache-size",
        str(-args.cache_size),
        "--mmap",
        str(args.mmap),
    ]
    if update:
        argv.append("--update")
        if args.multitrans:
            argv.append("--multitrans")
    if args.nosync:
        argv.append("--nosync")
    return argv


def _run(node: str, argv: List[str]) -> tuple[str, str]:
    return node, sqlite_tools.run_command(argv).stdout


def task(t: cluster.Task) -> None:
    build_dir = sqlite_tools.build_directory(t.args.build_dir, "kvtest")
    build_info = None
    if t.leader:
        sources = sqlite_tools.ensure_sources(build_dir, "kvtest", t.args.force_download)
        _, provenance = sqlite_tools.build(
            build_dir, "kvtest", sources, t.args.force_download
        )
        build_info = {
            "compiler": provenance["compiler_version"],
            "source_sha256": provenance["source_sha256"],
            "binary_sha256": provenance["binary_sha256"],
            "sqlite_version": sqlite_tools.SQLITE_VERSION,
        }
    t.node_barrier()
    t.node_info(build_info)

    binary = build_dir / "kvtest"
    db_path = t.task_dir / DB_FILENAME
    # Initialization is preparation, never timed; each task owns this database.
    sqlite_tools.run_command(init_argv(binary, db_path, t.args))

    keys = {
        "jmode": t.args.jmode,
        "sync": "off" if t.args.nosync else "full",
        "transaction": "autocommit" if t.args.multitrans else "batch",
        "payload_bytes": t.args.size,
        "variance_bytes": t.args.variance,
        "count": t.args.count,
        "cache_size_kib": t.args.cache_size,
        "mmap_bytes": t.args.mmap,
    }
    observed: Dict[tuple[int, int], Dict[str, Any]] = {}
    for repeat in range(1, t.args.repeats + 1):
        # kvtest overwrites the same rows again each time; later repeats
        # therefore measure overwrite-with-existing-rows. All timed writes
        # finish before the standalone read phase starts.
        _, write_results = t.batch(
            _run, t.node, run_argv(binary, db_path, t.args, update=True)
        )
        if t.index != 0:
            continue
        for sample, (node, stdout) in enumerate(write_results):
            where = f"task {sample} on {node}"
            written = parse_kvtest_output(stdout, update=True)
            if written["sqlite_version"] != sqlite_tools.SQLITE_VERSION:
                raise sqlite_tools.BenchmarkError(
                    f"{where} reported SQLite {written['sqlite_version']}, "
                    f"expected {sqlite_tools.SQLITE_VERSION}"
                )
            verify_output(written, t.args, update=True, where=where)
            observed[(repeat, sample)] = {
                "repeat": repeat,
                "sample": sample,
                "host": node,
                "journal_mode": written["journal_mode"],
                "cache_size": written["cache_size"],
                "synchronous": written["synchronous"],
                "page_size": written["page_size"],
                "completed_updates": written["completed_updates"],
                "attempted_updates": written["attempted_updates"],
                "committed_transactions": written["committed_transactions"],
            }
            t.emit(
                "repeats",
                {
                    **keys,
                    "tasks": t.count,
                    "repeat": repeat,
                    "sample": sample,
                    "host": node,
                },
                {
                    "microseconds_per_write": written["microseconds_per_write"],
                    "write_rate_mb_s": written["write_rate_mb_s"],
                    "total_elapsed_s": written["total_elapsed_s"],
                },
            )

    for repeat in range(1, t.args.repeats + 1):
        # Read-only invocation of the same database after all writes.
        _, read_results = t.batch(
            _run, t.node, run_argv(binary, db_path, t.args, update=False)
        )
        if t.index != 0:
            continue
        for sample, (node, stdout) in enumerate(read_results):
            where = f"task {sample} on {node}"
            read = parse_kvtest_output(stdout, update=False)
            if read["sqlite_version"] != sqlite_tools.SQLITE_VERSION:
                raise sqlite_tools.BenchmarkError(
                    f"{where} reported SQLite {read['sqlite_version']}, "
                    f"expected {sqlite_tools.SQLITE_VERSION}"
                )
            verify_output(read, t.args, update=False, where=where)
            entry = observed[(repeat, sample)]
            if entry["host"] != node:
                raise sqlite_tools.BenchmarkError(
                    f"read phase reported {node}, expected {entry['host']}"
                )
            entry["completed_reads"] = read["completed_reads"]
            entry["attempted_reads"] = read["attempted_reads"]
            t.emit(
                "reads",
                {
                    **keys,
                    "tasks": t.count,
                    "repeat": repeat,
                    "sample": sample,
                    "host": node,
                },
                {
                    "microseconds_per_read": read["microseconds_per_read"],
                    "read_rate_mb_s": read["read_rate_mb_s"],
                    "total_elapsed_s": read["total_elapsed_s"],
                },
            )

    # One final validation per task after all timed runs, using the same binary.
    # Any failure aborts the job before task 0 reports run facts.
    parse_integrity_check(
        sqlite_tools.run_command([str(binary), "stat", str(db_path)]).stdout
    )
    t.barrier()
    if t.index == 0:
        t.info({
            "sqlite_version": sqlite_tools.SQLITE_VERSION,
            "integrity_check": "ok",
            "observed": [observed[key] for key in sorted(observed)],
        })


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    cluster.run_tasks(ctx, args)
