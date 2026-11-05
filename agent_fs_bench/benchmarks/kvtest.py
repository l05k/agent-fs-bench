#!/usr/bin/env python3
"""Run SQLite's official single-commit BLOB overwrite microbenchmark.

The harness downloads and compiles SQLite's ``test/kvtest.c`` and runs
``kvtest run --update --random --multitrans``. Each BLOB overwrite is an
autocommit transaction. This isolates a controlled SQLite write workload;
it is not an OpenClaw transcript replay or an application-latency estimate.

OpenClaw's per-agent SQLite store motivates studying transactional storage.
The reference revision is 5f6f15fed0d6ba618cf781c9686a8b93f499564b; see
``docs/bench-kvtest-openclaw.md`` for source links and a calibration plan.
Static analysis establishes schema and configured persistence policy, not
event-size frequencies, statements per transaction, or transactions per turn.
CONFIG sizes and row counts are synthetic controls, not measured OpenClaw data.

Important differences: kvtest overwrites existing rows in one table, omits
transcript metadata/FTS/compression, and offers SQLite's default synchronization
(normally FULL) or ``--nosync`` (OFF), not the reference agent store's NORMAL
setting. Its WAL autocheckpoint is not the application's worker maintenance policy.
OpenClaw also selects rollback journaling for some storage types; this harness
does not reproduce that policy. Compare targets under the same declared SQLite
settings, rather than interpreting their numbers as equivalent application runs.

Reported microseconds/write amortize connection setup and final close/checkpoint;
they are not individually sampled transaction latencies. JSONL records the actual
journal mode, cache size and page size printed by kvtest. Other settings remain
requests. A fresh database directory is allocated for each invocation.

The workload uses the standard library; the runner needs DuckDB for CSV export.
A C compiler and network access are needed for the first source download/build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


from agent_fs_bench.results import PROJECT_ROOT, RunContext

SQLITE_VERSION = "3.54.1"
SQLITE_AMALGAMATION = f"sqlite-amalgamation-3540100.zip"
SQLITE_AMALGAMATION_URL = (
    "https://sqlite.org/2026/" + SQLITE_AMALGAMATION
)
KVTEST_URL = (
    "https://raw.githubusercontent.com/sqlite/sqlite/version-"
    f"{SQLITE_VERSION}/test/kvtest.c"
)

DB_FILENAME = "kv-openclaw.sqlite"
BUILD_METADATA_FILENAME = "kvtest-build.json"
JMODE_CHOICES = ("delete", "truncate", "persist", "memory", "wal", "off")

DEFAULT_PAYLOAD = 2048
DEFAULT_VARIANCE = 1024
DEFAULT_COUNT = 2000
DEFAULT_CACHE_KIB = 1000

# Deliberate experiment settings. Edit for an ablation; workload values are
# recorded in every JSONL row. build_dir and force_download are run management
# and are not recorded. Private settings must never be committed.
CONFIG = {
    "size": DEFAULT_PAYLOAD,
    "variance": DEFAULT_VARIANCE,
    "count": DEFAULT_COUNT,
    "cache_size": DEFAULT_CACHE_KIB,
    "jmode": "wal",
    "nosync": False,
    "mmap": 0,
    "repeats": 1,
    "build_dir": None,
    "force_download": False,
}


class BenchmarkError(RuntimeError):
    pass


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    """Validate and normalize settings shared by CLI and direct callers."""
    mode = getattr(args, "jmode", None)
    if not isinstance(mode, str):
        raise BenchmarkError(
            f"invalid jmode {mode!r}; expected one of {', '.join(JMODE_CHOICES)}"
        )
    normalized = mode.lower()
    if normalized not in JMODE_CHOICES:
        raise BenchmarkError(
            f"invalid jmode {mode!r}; expected one of {', '.join(JMODE_CHOICES)}"
        )
    args.jmode = normalized
    return args


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run SQLite's official kvtest with --multitrans against the "
            "filesystem under test."
        )
    )
    parser.parse_args(argv)
    return normalize_args(argparse.Namespace(**CONFIG))




def ensure_directory(path: Path, what: str) -> None:
    """Create ``path`` with an actionable error when that is impossible."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BenchmarkError(f"cannot create {what} {path}: {exc}") from exc


def download(url: str, destination: Path) -> None:
    """Download ``url`` to ``destination`` atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            partial.write_bytes(response.read())
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise BenchmarkError(f"failed to download {url}: {exc}") from exc
    partial.replace(destination)


def ensure_sources(build_dir: Path, force: bool) -> Dict[str, Path]:
    """Fetch the amalgamation and ``kvtest.c``; return both paths."""
    ensure_directory(build_dir, "build directory")
    archive = build_dir / SQLITE_AMALGAMATION
    source = build_dir / "kvtest.c"
    if force or not archive.exists():
        download(SQLITE_AMALGAMATION_URL, archive)
    if force or not source.exists():
        download(KVTEST_URL, source)

    inner = archive.with_suffix("")
    amalgamation = inner / "sqlite3.c"
    header = inner / "sqlite3.h"
    if force or not amalgamation.exists() or not header.exists():
        if inner.exists():
            shutil.rmtree(inner)
        shutil.unpack_archive(str(archive), str(build_dir))
        if not amalgamation.exists():
            raise BenchmarkError(
                f"{archive} did not contain sqlite3.c as expected"
            )
    return {"kvtest": source, "sqlite3": amalgamation, "sqlite3_h": header}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(sources: Dict[str, Path]) -> Dict[str, str]:
    return {name: file_sha256(path) for name, path in sources.items()}


def resolve_compiler() -> str:
    requested = os.environ.get("CC")
    if requested:
        compiler = shutil.which(requested)
        if compiler:
            return compiler
        raise BenchmarkError(
            f"C compiler {requested!r} from CC was not found or is not executable"
        )
    for name in ("cc", "gcc"):
        compiler = shutil.which(name)
        if compiler:
            return compiler
    raise BenchmarkError(
        "no C compiler found; set CC or install cc/gcc on the test machine"
    )


def compiler_version(compiler: str) -> str:
    try:
        proc = subprocess.run(
            [compiler, "--version"], capture_output=True, text=True
        )
    except OSError as exc:
        raise BenchmarkError(
            f"cannot run C compiler {compiler}: {exc}"
        ) from exc
    output = (proc.stdout.strip() or proc.stderr.strip())
    if proc.returncode != 0 or not output:
        raise BenchmarkError(
            f"cannot determine the version of C compiler {compiler} "
            f"(exit {proc.returncode}); refusing to build without provenance"
            + (f":\n{output}" if output else "")
        )
    return output.splitlines()[0]


def load_build_metadata(
    metadata_path: Path, binary: Path
) -> Dict[str, Any]:
    """Load provenance and reject anything not bound to ``binary``."""
    rebuild = (
        "rebuild once with CONFIG['force_download'] = True "
        f"or delete {binary} and {metadata_path}"
    )
    try:
        text = metadata_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BenchmarkError(
            f"cached binary {binary} has no build provenance; {rebuild}"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise BenchmarkError(
            f"cannot read build provenance {metadata_path}: {exc}; {rebuild}"
        ) from exc
    try:
        metadata = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(
            f"invalid build provenance {metadata_path}: {exc}; {rebuild}"
        ) from exc
    if not isinstance(metadata, dict):
        raise BenchmarkError(
            f"invalid build provenance {metadata_path}: expected an object; "
            + rebuild
        )
    required = (
        "binary_sha256",
        "source_sha256",
        "compiler",
        "compiler_argv",
        "compiler_version",
    )
    missing = [name for name in required if name not in metadata]
    if missing:
        raise BenchmarkError(
            f"incomplete build provenance {metadata_path}: missing "
            + ", ".join(missing)
            + f"; {rebuild}"
        )
    if (
        not isinstance(metadata["binary_sha256"], str)
        or not isinstance(metadata["source_sha256"], dict)
        or not isinstance(metadata["compiler"], str)
        or not isinstance(metadata["compiler_version"], str)
        or not isinstance(metadata["compiler_argv"], list)
        or not metadata["compiler_argv"]
    ):
        raise BenchmarkError(
            f"invalid build provenance {metadata_path}; {rebuild}"
        )
    if metadata["binary_sha256"] != file_sha256(binary):
        raise BenchmarkError(
            f"cached binary {binary} does not match its build provenance; "
            + rebuild
        )
    return metadata


def build(
    build_dir: Path, sources: Dict[str, Path], force: bool
) -> Tuple[Path, Dict[str, Any]]:
    """Compile ``kvtest`` and return the binary with its build provenance."""
    binary = build_dir / "kvtest"
    metadata_path = build_dir / BUILD_METADATA_FILENAME
    current_hashes = source_hashes(sources)
    newest_source = max(path.stat().st_mtime for path in sources.values())
    if binary.exists() and not force and binary.stat().st_mtime >= newest_source:
        metadata = load_build_metadata(metadata_path, binary)
        if metadata["source_sha256"] == current_hashes:
            return binary, metadata

    compiler = resolve_compiler()
    version = compiler_version(compiler)
    # Compile to a temporary path so a failed compile or provenance write
    # cannot replace or bless the cached binary.
    staged = build_dir / "kvtest.tmp"
    metadata_tmp = build_dir / (BUILD_METADATA_FILENAME + ".tmp")
    command = [
        compiler,
        "-O2",
        "-I",
        str(sources["sqlite3_h"].parent),
        "-o",
        str(staged),
        str(sources["kvtest"]),
        str(sources["sqlite3"]),
        "-DSQLITE_THREADSAFE=0",
        "-DSQLITE_OMIT_LOAD_EXTENSION",
        "-lm",
    ]
    try:
        try:
            proc = subprocess.run(command, capture_output=True, text=True)
        except OSError as exc:
            raise BenchmarkError(
                f"cannot run C compiler {compiler}: {exc}"
            ) from exc
        if proc.returncode != 0:
            raise BenchmarkError(
                "failed to compile kvtest:\n"
                + shlex.join(command)
                + "\n"
                + proc.stdout
                + proc.stderr
            )
        if not staged.is_file():
            raise BenchmarkError(
                f"C compiler {compiler} did not produce {staged}"
            )
        metadata = {
            "built_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "binary_sha256": file_sha256(staged),
            "source_sha256": current_hashes,
            "compiler": compiler,
            "compiler_argv": command,
            "compiler_version": version,
        }
        metadata_tmp.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staged.replace(binary)
        metadata_tmp.replace(metadata_path)
    except OSError as exc:
        raise BenchmarkError(f"cannot save kvtest build: {exc}") from exc
    finally:
        staged.unlink(missing_ok=True)
        metadata_tmp.unlink(missing_ok=True)
    return binary, metadata


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchmarkError(
            f"command failed ({proc.returncode}): {shlex.join(command)}\n"
            + proc.stdout
            + proc.stderr
        )
    return proc


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
        raise BenchmarkError(
            "could not parse kvtest output; missing "
            + ", ".join(missing)
            + ". Full output:\n"
            + text
        )
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
        raise BenchmarkError(
            "could not parse kvtest stat output; missing "
            "'Integrity-check'. Full output:\n" + text
        )
    failed = [value for value in values if value != "ok"]
    if failed:
        raise BenchmarkError(
            "kvtest integrity-check failed: "
            + "; ".join(failed)
            + "\nFull output:\n"
            + text
        )
    return "ok"


def remove_database(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def measure(args: argparse.Namespace) -> Dict[str, Any]:
    normalize_args(args)
    build_dir = args.build_dir or (PROJECT_ROOT / "build" / "kvtest")
    sources = ensure_sources(build_dir, args.force_download)
    binary, provenance = build(build_dir, sources, args.force_download)

    target_dir = args.target_dir
    ensure_directory(target_dir, "target directory")
    db_path = target_dir / DB_FILENAME
    remove_database(db_path)

    run_command(
        [
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
    )

    run_argv = [
        str(binary),
        "run",
        str(db_path),
        "--count",
        str(args.count),
        # Every BLOB overwrite commits in its own transaction.
        "--update",
        "--random",
        "--multitrans",
        "--jmode",
        args.jmode,
        # Negative cache_size means KiB; kvtest reads the value back.
        "--cache-size",
        str(-args.cache_size),
        "--mmap",
        str(args.mmap),
    ]
    if args.nosync:
        run_argv.append("--nosync")

    repeats: List[Dict[str, Any]] = []
    for index in range(args.repeats):
        # kvtest overwrites the same rows again each time; later repeats
        # therefore measure overwrite-with-existing-rows.
        proc = run_command(run_argv)
        repeats.append(
            {
                "repeat": index + 1,
                "parsed": parse_kvtest_output(proc.stdout),
            }
        )

    # One final validation after all timed runs, using the same binary.
    integrity_check = parse_integrity_check(
        run_command([str(binary), "stat", str(db_path)]).stdout
    )

    return {
        # Requests only; parsed output carries the values SQLite read back.
        "requests": {
            "size": args.size,
            "variance": args.variance,
            "count": args.count,
            "cache_size_kib": args.cache_size,
            "jmode": args.jmode,
            "nosync": args.nosync,
            "mmap_bytes": args.mmap,
            "repeats": args.repeats,
        },
        "provenance": provenance,
        "repeats": repeats,
        "integrity_check": integrity_check,
    }


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    args.target_dir = ctx.work_dir
    if args.build_dir is not None:
        args.build_dir = args.build_dir.expanduser().resolve()
    result = measure(args)
    provenance = result["provenance"]
    repeats = result["repeats"]
    integrity_check = result["integrity_check"]
    sqlite_version = repeats[0]["parsed"]["sqlite_version"]
    if any(
        entry["parsed"]["sqlite_version"] != sqlite_version
        for entry in repeats
    ):
        raise BenchmarkError(
            "kvtest reported different SQLite versions across repeats"
        )

    ctx.parameters.clear()
    ctx.parameters.update(result["requests"])
    ctx.parameters["tool_versions"] = {
        "sqlite": sqlite_version,
        "compiler": provenance["compiler_version"],
    }
    ctx.parameters["source_sha256"] = provenance["source_sha256"]
    ctx.parameters["binary_sha256"] = provenance["binary_sha256"]

    for entry in repeats:
        parsed = entry["parsed"]
        ctx.emit(
            "multitrans-overwrite",
            {
                "microseconds_per_write": parsed["microseconds_per_write"],
                "write_rate_mb_s": parsed["write_rate_mb_s"],
                "total_elapsed_s": parsed["total_elapsed_s"],
            },
            repeat=entry["repeat"],
            details={
                "observed": {
                    "journal_mode": parsed["journal_mode"],
                    "cache_size": parsed["cache_size"],
                    "page_size": parsed["page_size"],
                },
                "integrity_check": integrity_check,
            },
        )
