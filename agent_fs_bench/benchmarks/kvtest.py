#!/usr/bin/env python3
"""Run SQLite's official ``kvtest --multitrans`` write benchmark.

This is a thin harness around SQLite's own ``test/kvtest.c``. It does not
reimplement the workload: it downloads the official sources, compiles them on
the test machine, and runs ``kvtest run --update --multitrans`` so every BLOB
overwrite commits in its own transaction. That commit shape is the closest
match in SQLite's shipped test programs to OpenClaw's per-turn transcript
write (4 events + identities + index state + FTS, one commit).

Why ``kvtest`` and not ``speedtest1``: ``speedtest1`` batches ~750 statements
per transaction, so it mostly measures B-tree throughput, not commit cost.

Deliberate gaps against OpenClaw (keep these in mind when reading the numbers):

  * OpenClaw has 12 indexes across 7 tables in the write path; ``kv`` has one
    primary key and no secondary index.
  * OpenClaw writes an FTS5 projection in the same transaction (30-40% of the
    cost in the transcript workload); ``kvtest`` writes no FTS.
  * OpenClaw payloads are 90% 1-4 KiB, 9% 256 KiB, 1% 1 MiB JSON text;
    ``kvtest`` writes fixed-size BLOBs varied only by ``--variance``.

Pragma matching against OpenClaw's agent database
(``src/infra/sqlite-wal.ts`` + ``src/state/openclaw-agent-db.ts`` at
openclaw@5f6f15f):

  parameter              OpenClaw                     reached here
  ---------------------  ---------------------------  -----------------------
  journal_mode           WAL                          yes (--jmode wal)
  synchronous            NORMAL                       NO  (kvtest offers only
                                                      default FULL or --nosync)
  busy_timeout           5000                         NO  (not configurable)
  foreign_keys           ON                           n/a (single table)
  journal_size_limit     64 MiB                       NO  (not configurable)
  auto_vacuum            INCREMENTAL (fresh db)       NO  (init cannot set it)
  wal_autocheckpoint     0 (worker-maintained)        1000 (SQLite default)
  cache_size             (default)                    --cache-size, default
                                                      1000 KiB
  mmap_size              (default)                    --mmap, default 0

``wal_autocheckpoint`` is the value that matters most and it cannot be pinned
to OpenClaw's 0 through ``kvtest``'s options: ``--nocheckpoint`` sets it to 0,
which lets the WAL grow without bound and pushes one huge checkpoint to the
end of the run. This harness therefore leaves the SQLite default of 1000
pages (roughly 4 MiB at a 4 KiB page size) and records it in the result.

Reported microseconds/write amortize connection setup and the final close /
checkpoint as well as the writes. This is a single-table comparison workload,
not a strict OpenClaw latency lower bound (FULL sync is stronger than NORMAL).
The shared runner allocates a fresh database directory for every invocation.

The workload uses the standard library; the shared runner needs DuckDB for its
default post-run CSV summary. A C compiler and network access are needed for
the first run of a given source revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


from agent_fs_bench.results import PROJECT_ROOT, RunContext
MIB = 1024 * 1024

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

# OpenClaw agent database settings this harness cannot reproduce through
# kvtest's command line. Recorded verbatim so the gap is visible in results.
OPENCLAW_SETTINGS = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "busy_timeout": 5000,
    "foreign_keys": "ON",
    "journal_size_limit": 64 * MIB,
    "wal_autocheckpoint": 0,
}

# What kvtest will actually run with, given the options this harness passes.
REACHED_SETTINGS = {
    "journal_mode": "WAL",
    "synchronous": "FULL",
    "wal_autocheckpoint": 1000,
}

DEFAULT_PAYLOAD = 2048
DEFAULT_VARIANCE = 1024
DEFAULT_COUNT = 2000
DEFAULT_CACHE_KIB = 1000

# Deliberate experiment settings. Edit for an ablation; values are recorded in
# every JSONL row, and private settings must never be committed.
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
    "microseconds_per_write": re.compile(r"Microseconds per BLOB write:\s*([0-9.]+)"),
    "write_rate_mb_s": re.compile(r"Content write rate:\s*([0-9.]+)"),
    "total_elapsed_s": re.compile(r"Total elapsed time:\s*([0-9.]+)"),
    "page_size": re.compile(r"Database page size:\s*(\d+)"),
    "journal_mode": re.compile(r"--jmode\s+(\w+)"),
}


def parse_kvtest_output(text: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for name, pattern in PARSE_PATTERNS.items():
        match = pattern.search(text)
        if match:
            value = match.group(1)
            if name == "journal_mode":
                parsed[name] = value.upper()
            else:
                parsed[name] = float(value) if "." in value else int(value)
    if "microseconds_per_write" not in parsed:
        raise BenchmarkError(
            "could not parse kvtest output; expected "
            "'Microseconds per BLOB write'. Full output:\n" + text
        )
    return parsed


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def reached_synchronous(args: argparse.Namespace) -> str:
    """Return the synchronous mode kvtest will run with."""
    return "OFF" if args.nosync else "FULL"


def database_pragmas(
    db_path: Path, reached_synchronous: str
) -> Dict[str, Any]:
    """Read back the pragmas that were actually in effect.

    ``synchronous``, ``wal_autocheckpoint``, ``cache_size`` and ``mmap_size``
    are connection-scoped in SQLite, so a fresh inspection connection cannot
    observe the values the benchmark used. Those are reported from the
    invocation instead; only the persistent header and page counts are read.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        values: Dict[str, Any] = {}
        for name in (
            "journal_mode",
            "journal_size_limit",
            "page_size",
            "page_count",
            "freelist_count",
        ):
            try:
                row = conn.execute(f"PRAGMA {name}").fetchone()
                values[name] = row[0] if row else None
            except sqlite3.Error as exc:
                values[name] = f"unavailable: {exc}"
        try:
            values["integrity_check"] = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        except sqlite3.Error as exc:
            values["integrity_check"] = f"unavailable: {exc}"
        values["synchronous_name"] = reached_synchronous
        return values
    finally:
        conn.close()


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

    init_command = [
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
    setup_started = time.perf_counter()
    init_proc = run_command(init_command)
    setup_elapsed = time.perf_counter() - setup_started

    conn = sqlite3.connect(str(db_path))
    try:
        db_bytes_after_init = file_size(db_path)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()

    run_command_argv = [
        str(binary),
        "run",
        str(db_path),
        "--count",
        str(args.count),
        "--update",
        "--random",
        "--multitrans",
        "--jmode",
        args.jmode,
        "--cache-size",
        str(-args.cache_size),
        "--mmap",
        str(args.mmap),
    ]
    if args.nosync:
        run_command_argv.append("--nosync")

    repeats: List[Dict[str, Any]] = []
    for index in range(args.repeats):
        # kvtest writes the same rows again each time; that is intentional,
        # so later repeats measure overwrite-with-existing-rows.
        wal_path = Path(str(db_path) + "-wal")
        before_wal = file_size(wal_path)
        proc = run_command(run_command_argv)
        parsed = parse_kvtest_output(proc.stdout)
        repeats.append(
            {
                "index": index + 1,
                "argv": run_command_argv,
                "parsed": parsed,
                "wal_bytes_before": before_wal,
                "wal_bytes_after": file_size(wal_path),
                "raw_stdout": proc.stdout,
            }
        )

    final_wal_path = Path(str(db_path) + "-wal")
    final_shm_path = Path(str(db_path) + "-shm")
    db_pragmas = database_pragmas(db_path, reached_synchronous(args))
    db_bytes_final = file_size(db_path)
    wal_bytes_final = file_size(final_wal_path)
    shm_bytes_final = file_size(final_shm_path)


    return {
        "workload": "sqlite-kvtest-openclaw-multitrans",
        "status": "ok" if db_pragmas.get("integrity_check") == "ok" else "failed",
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_build_version": SQLITE_VERSION,
        "kvtest_source": KVTEST_URL,
        "amalgamation_source": SQLITE_AMALGAMATION_URL,
        "cc": provenance["compiler"],
        "compiler_argv": provenance["compiler_argv"],
        "compiler_version": provenance["compiler_version"],
        "source_sha256": provenance["source_sha256"],
        "binary_sha256": provenance["binary_sha256"],
        "target_dir": str(target_dir),
        "db_path": str(db_path),
        "payload_bytes": args.size,
        "payload_variance": args.variance,
        "writes": args.count,
        "jmode": args.jmode,
        "nosync": args.nosync,
        "cache_size_kib": args.cache_size,
        "mmap_bytes": args.mmap,
        "repeats": args.repeats,
        "setup_elapsed_s": setup_elapsed,
        "page_size": db_pragmas.get("page_size") or page_size,
        "openclaw_settings": OPENCLAW_SETTINGS,
        "reached_settings": {
            **REACHED_SETTINGS,
            "journal_mode": repeats[-1]["parsed"]["journal_mode"],
            "synchronous": reached_synchronous(args),
            "cache_size": -args.cache_size,
            "mmap_size": args.mmap,
        },
        "settings_gap": [
            "synchronous: OpenClaw NORMAL, kvtest offers only FULL or OFF",
            "busy_timeout: OpenClaw 5000 ms, kvtest cannot set it",
            "journal_size_limit: OpenClaw 64 MiB, kvtest cannot set it",
            (
                "wal_autocheckpoint: OpenClaw 0, kvtest default 1000"
                if repeats[-1]["parsed"]["journal_mode"] == "WAL"
                else "WAL checkpointing: not applicable to the reached journal mode"
            ),
            "auto_vacuum: OpenClaw INCREMENTAL on fresh db, kvtest cannot set it",
            "no secondary indexes, no FTS5, single kv table",
        ],
        "results": repeats,
        "pragmas": db_pragmas,
        "files": {
            "db_bytes_after_init": db_bytes_after_init,
            "db_bytes_final": db_bytes_final,
            "wal_bytes_final": wal_bytes_final,
            "shm_bytes_final": shm_bytes_final,
        },
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "init_stdout": init_proc.stdout,
        "init_argv": init_command,
    }


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    args.target_dir = ctx.work_dir
    if args.build_dir is not None:
        args.build_dir = args.build_dir.expanduser().resolve()
    result = measure(args)
    if result["status"] != "ok":
        raise BenchmarkError(f"integrity_check={result['pragmas']['integrity_check']}")
    repeats = result.pop("results")
    ctx.parameters.pop("build_dir", None)
    ctx.parameters["jmode"] = result["jmode"]
    ctx.parameters.update({name: result[name] for name in (
        "sqlite_build_version", "cc", "compiler_argv",
        "compiler_version", "source_sha256", "binary_sha256"
    )})
    for entry in repeats:
        parsed = entry["parsed"]
        metrics = {name: value for name, value in parsed.items()
                   if isinstance(value, (int, float)) and not isinstance(value, bool)}
        details = {**result, **entry,
                   "fsync_mode": f"SQLite synchronous={reached_synchronous(args)}",
                   "cache_state": "warm: initialized database; random-key overwrites; no cache drop"}
        ctx.emit("multitrans-overwrite", metrics, repeat=entry["index"], details=details)
