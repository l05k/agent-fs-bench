"""Pinned official SQLite sources and build provenance for SQL workloads."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, Sequence, Tuple
import urllib.request

from agent_fs_bench.results import PROJECT_ROOT

SQLITE_VERSION = "3.54.1"
SQLITE_AMALGAMATION = "sqlite-amalgamation-3540100.zip"
SQLITE_AMALGAMATION_URL = "https://sqlite.org/2026/" + SQLITE_AMALGAMATION
# Cross-checked against the SHA3-verified 3.54.1 release archives:
# https://sqlite.org/download.html and https://sqlite.org/releaselog/3_54_1.html
DOWNLOAD_SHA256 = {
    SQLITE_AMALGAMATION: "0a9c8e34352dad46006aa82836d5383bff9dc6bc9ce5a60553c05aa30e6ad1f9",
    "kvtest.c": "72ed2071ce76ff6033729cde69e0e61c8723748d1a1f35b9f8a865b5f7ab2424",
    "speedtest1.c": "bcb4a3123b60db648e748837f5c46092e23a3b35d970f7da5e62c42809f18185",
}
COMPILE_FLAGS = (
    "-O2",
    "-DSQLITE_THREADSAFE=1",
    "-DSQLITE_OMIT_LOAD_EXTENSION",
    "-DSQLITE_DEFAULT_SYNCHRONOUS=2",
    "-DSQLITE_DEFAULT_WAL_SYNCHRONOUS=2",
    "-lm",
    "-pthread",
)

class BenchmarkError(RuntimeError):
    pass

def build_directory(configured: str | Path | None, name: str) -> Path:
    """Resolve a build cache on this machine; None means this checkout's build/<name>."""
    return Path(configured or PROJECT_ROOT / "build" / name).expanduser().resolve()

def download(url: str, destination: Path, expected_sha256: str) -> None:
    """Verify fixed source content before atomically replacing the cache."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            partial.write_bytes(response.read())
        if file_sha256(partial) != expected_sha256:
            raise BenchmarkError(f"SHA-256 mismatch for {destination.name}")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)

def ensure_sources(build_dir: Path, tool: str, force: bool) -> Dict[str, Path]:
    """Fetch the pinned amalgamation and official test program."""
    build_dir.mkdir(parents=True, exist_ok=True)
    archive = build_dir / SQLITE_AMALGAMATION
    source = build_dir / f"{tool}.c"
    for destination, url in (
        (archive, SQLITE_AMALGAMATION_URL),
        (source, f"https://sqlite.org/src/raw/test/{tool}.c?ci=version-{SQLITE_VERSION}"),
    ):
        expected = DOWNLOAD_SHA256[destination.name]
        if force or not destination.exists():
            download(url, destination, expected)
        elif file_sha256(destination) != expected:
            raise BenchmarkError(
                f"SHA-256 mismatch for cached {destination.name}; "
                "set CONFIG['force_download'] = True to refresh"
            )

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
    return {tool: source, "sqlite3": amalgamation, "sqlite3_h": header}

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
    proc = subprocess.run(
        [compiler, "--version"], capture_output=True, text=True
    )
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
    build_dir: Path, tool: str, sources: Dict[str, Path], force: bool
) -> Tuple[Path, Dict[str, Any]]:
    """Compile an official tool and bind its binary to sources and build flags."""
    binary = build_dir / tool
    metadata_path = build_dir / f"{tool}-build.json"
    current_hashes = source_hashes(sources)
    newest_source = max(path.stat().st_mtime for path in sources.values())
    if binary.exists() and not force and binary.stat().st_mtime >= newest_source:
        metadata = load_build_metadata(metadata_path, binary)
        if (metadata["source_sha256"] == current_hashes
                and metadata.get("compile_flags") == list(COMPILE_FLAGS)):
            return binary, metadata

    compiler = resolve_compiler()
    version = compiler_version(compiler)
    # Compile to a temporary path so a failed compile or provenance write
    # cannot replace or bless the cached binary.
    staged = build_dir / f"{tool}.tmp"
    metadata_tmp = metadata_path.with_suffix(".json.tmp")
    command = [
        compiler,
        "-I",
        str(sources["sqlite3_h"].parent),
        "-o",
        str(staged),
        str(sources[tool]),
        str(sources["sqlite3"]),
        *COMPILE_FLAGS,
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
        if proc.returncode != 0:
            raise BenchmarkError(
                f"failed to compile {tool}:\n"
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
            "compile_flags": list(COMPILE_FLAGS),
        }
        metadata_tmp.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staged.replace(binary)
        metadata_tmp.replace(metadata_path)
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
