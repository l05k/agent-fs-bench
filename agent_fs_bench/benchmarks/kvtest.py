#!/usr/bin/env python3
"""Run SQLite's official single-writer BLOB overwrite microbenchmark.

The harness compiles SQLite's ``test/kvtest.c`` and runs random overwrites.
``multitrans=True`` commits each overwrite; False batches the entire run.
The default NAS baseline uses DELETE journaling and FULL synchronization.
It is not an OpenClaw transcript replay or an application-latency estimate.

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
Never run concurrent writers/readers against the measured database: official
kvtest can report writes despite failed commits; integrity_check is not proof
of committed work. A workload invocation owns its database exclusively.

Each timed invocation emits one ``repeats`` row: the experiment settings and
repeat index as key columns, then ``microseconds_per_write``,
``write_rate_mb_s`` and ``total_elapsed_s``. Reported microseconds/write
amortize connection setup and final close/checkpoint; they are not individually
sampled transaction latencies. The observed journal mode, cache size and page
size, the SQLite version, the build provenance and the final integrity-check
result go to the run manifest. A fresh database directory is allocated for each
invocation.

The workload uses the standard library. A C compiler and network access are
needed for the first source download/build.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence

from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext

from . import sqlite_tools

DB_FILENAME = "kvtest.sqlite"

DEFAULT_PAYLOAD = 2048
DEFAULT_VARIANCE = 0
DEFAULT_COUNT = 2000
DEFAULT_CACHE_KIB = 1000

# Deliberate experiment settings. Edit for an ablation; they become the
# per-row key columns. build_dir and force_download are run management.
# Private settings must never be committed.
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
    "build_dir": None,
    "force_download": False,
}

FIGURES = [
    Figure(
        "repeats", "transaction", "microseconds_per_write", kind="box",
        xlabel="Transaction mode", ylabel="Microseconds per write",
        name="kvtest-microseconds-per-write",
    ),
]

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run SQLite's official single-writer kvtest against the filesystem under test."
        )
    )
    parser.parse_args(argv)
    args = argparse.Namespace(**CONFIG)
    args.jmode = args.jmode.lower()
    return args

PARSE_PATTERNS = {
    "sqlite_version": re.compile(
        r"^SQLite version:[ \t]*(\S+)[ \t]*\r?$", re.MULTILINE
    ),
    "cache_size_and_journal_mode": re.compile(
        r"^--cache-size[ \t]+(-?\d+)[ \t]+--jmode[ \t]+(\w+)[ \t]*\r?$",
        re.MULTILINE,
    ),
    "page_size": re.compile(
        r"^Database page size:[ \t]*(\d+)[ \t]*\r?$", re.MULTILINE
    ),
    "total_elapsed_s": re.compile(
        r"^Total elapsed time:[ \t]*([0-9]+\.[0-9]+)[ \t]*\r?$",
        re.MULTILINE,
    ),
    "microseconds_per_write": re.compile(
        r"^Microseconds per BLOB write:[ \t]*([0-9]+\.[0-9]+)[ \t]*\r?$",
        re.MULTILINE,
    ),
    "write_rate_mb_s": re.compile(
        r"^Content write rate:[ \t]*([0-9]+\.[0-9]+)[ \t]*MB/s[ \t]*\r?$",
        re.MULTILINE,
    ),
}
INTEGRITY_PATTERN = re.compile(
    r"^Integrity-check:[ \t]*(\S.*?)[ \t]*\r?$", re.MULTILINE
)

def parse_kvtest_output(text: str) -> Dict[str, Any]:
    """Parse ``kvtest run`` output, rejecting any missing measured field.

    Only values kvtest itself prints are returned: library version, the
    effective journal mode, cache size and page size read back from SQLite,
    and the timing lines. ``--mmap`` is merely an echo of the request and
    ``synchronous`` is never printed, so neither can be reported as observed.
    """
    matches: Dict[str, re.Match] = {}
    missing: List[str] = []
    for name, pattern in PARSE_PATTERNS.items():
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
    return {
        "sqlite_version": matches["sqlite_version"].group(1),
        "journal_mode": matches["cache_size_and_journal_mode"].group(2),
        "cache_size": int(matches["cache_size_and_journal_mode"].group(1)),
        "page_size": int(matches["page_size"].group(1)),
        "total_elapsed_s": float(matches["total_elapsed_s"].group(1)),
        "microseconds_per_write": float(
            matches["microseconds_per_write"].group(1)
        ),
        "write_rate_mb_s": float(matches["write_rate_mb_s"].group(1)),
    }

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

def measure(args: argparse.Namespace, target_dir: Path) -> Dict[str, Any]:
    build_dir = Path(
        args.build_dir or (PROJECT_ROOT / "build" / "kvtest")
    ).expanduser().resolve()
    sources = sqlite_tools.ensure_sources(build_dir, "kvtest", args.force_download)
    binary, provenance = sqlite_tools.build(build_dir, "kvtest", sources, args.force_download)

    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / DB_FILENAME
    sqlite_tools.remove_database(db_path)

    sqlite_tools.run_command([
        str(binary),
        "init",
        str(db_path),
        "--count",
        str(args.count),
        "--size",
        str(args.size),
        "--variance",
        str(args.variance),
    ])

    run_argv = [
        str(binary),
        "run",
        str(db_path),
        "--count",
        str(args.count),
        "--update",
        "--random",
        "--jmode",
        args.jmode,
        # Negative cache_size means KiB; kvtest reads the value back.
        "--cache-size",
        str(-args.cache_size),
        "--mmap",
        str(args.mmap),
    ]
    if args.multitrans:
        run_argv.append("--multitrans")
    if args.nosync:
        run_argv.append("--nosync")

    repeats: List[Dict[str, Any]] = []
    for index in range(args.repeats):
        # kvtest overwrites the same rows again each time; later repeats
        # therefore measure overwrite-with-existing-rows.
        proc = sqlite_tools.run_command(run_argv)
        repeats.append(
            {
                "repeat": index + 1,
                "parsed": parse_kvtest_output(proc.stdout),
            }
        )

    # One final validation after all timed runs, using the same binary.
    integrity = parse_integrity_check(
        sqlite_tools.run_command([str(binary), "stat", str(db_path)]).stdout
    )

    return {
        "provenance": provenance,
        "repeats": repeats,
        "integrity_check": integrity,
    }

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    result = measure(args, ctx.work_dir)
    provenance = result["provenance"]
    repeats = result["repeats"]
    sqlite_version = repeats[0]["parsed"]["sqlite_version"]
    if any(
        entry["parsed"]["sqlite_version"] != sqlite_version
        for entry in repeats
    ):
        raise sqlite_tools.BenchmarkError("kvtest reported different SQLite versions across repeats")

    ctx.info.update(
        sqlite_version=sqlite_version,
        compiler=provenance["compiler_version"],
        source_sha256=provenance["source_sha256"],
        binary_sha256=provenance["binary_sha256"],
        integrity_check=result["integrity_check"],
        observed=[{
            "repeat": entry["repeat"],
            **{key: entry["parsed"][key] for key in ("journal_mode", "cache_size", "page_size")},
        } for entry in repeats],
    )

    keys = {
        "jmode": args.jmode,
        "sync": "off" if args.nosync else "full",
        "transaction": "autocommit" if args.multitrans else "batch",
        "payload_bytes": args.size,
        "variance_bytes": args.variance,
        "count": args.count,
        "cache_size_kib": args.cache_size,
        "mmap_bytes": args.mmap,
    }
    for entry in repeats:
        parsed = entry["parsed"]
        ctx.emit(
            "repeats",
            {**keys, "repeat": entry["repeat"]},
            {
                "microseconds_per_write": parsed["microseconds_per_write"],
                "write_rate_mb_s": parsed["write_rate_mb_s"],
                "total_elapsed_s": parsed["total_elapsed_s"],
            },
        )
