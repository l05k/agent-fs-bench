"""Single-connection SQLite speedtest1 main workload on a mounted filesystem."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

from agent_fs_bench.results import PROJECT_ROOT, RunContext

from . import sqlite_tools

CONFIG = {
    "size": 100,
    "cache_size": 2000,  # KiB; passed as a negative PRAGMA cache_size.
    "page_size": 4096,
    "jmode": "delete",
    "nosync": False,
    "repeats": 5,
    "build_dir": None,
    "force_download": False,
}

# Official main stages at the pinned SQLite revision; reject partial output.
MAIN_STAGES = (
    100, 110, 120, 130, 140, 142, 145, 150, 160, 161, 170, 180, 190,
    200, 210, 230, 240, 250, 260, 270, 280, 290, 300, 310, 320,
    400, 410, 500, 510, 520, 980, 990,
)
VERSION_PATTERN = re.compile(r"^-- Speedtest1 for SQLite (\S+)", re.MULTILINE)
STAGE_PATTERN = re.compile(
    r"^[ \t]*(\d+) - (.+?)\.{2,}[ \t]+(\d+\.\d+)s[ \t]*\r?$",
    re.MULTILINE,
)
TOTAL_PATTERN = re.compile(r"^[ \t]*TOTAL\.+[ \t]+(\d+\.\d+)s[ \t]*\r?$", re.MULTILINE)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    args = argparse.Namespace(**CONFIG)
    args.jmode = sqlite_tools.normalize_journal_mode(args.jmode)
    if args.size < 1 or args.cache_size < 1 or args.repeats < 1:
        raise sqlite_tools.BenchmarkError("size, cache_size and repeats must be positive")
    if args.page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
        raise sqlite_tools.BenchmarkError("page_size must be a power of two from 512 to 65536")
    return args


def parse_speedtest_output(text: str) -> dict:
    version = VERSION_PATTERN.search(text)
    total = TOTAL_PATTERN.search(text)
    stages = [
        {"id": int(number), "description": description, "elapsed_s": float(seconds)}
        for number, description, seconds in STAGE_PATTERN.findall(text)
    ]
    if version is None or total is None:
        raise sqlite_tools.BenchmarkError("speedtest1 output is missing version or TOTAL")
    if tuple(stage["id"] for stage in stages) != MAIN_STAGES:
        raise sqlite_tools.BenchmarkError("speedtest1 output has incomplete or unexpected main stages")
    total_s = float(total.group(1))
    if abs(sum(stage["elapsed_s"] for stage in stages) - total_s) > 0.001:
        raise sqlite_tools.BenchmarkError("speedtest1 TOTAL does not match its stage timings")
    return {"sqlite_version": version.group(1), "stages": stages, "official_total_s": total_s}


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    build_dir = Path(args.build_dir or (PROJECT_ROOT / "build" / "sqlite-speedtest"))
    build_dir = build_dir.expanduser().resolve()
    sources = sqlite_tools.ensure_sources(build_dir, "speedtest1", args.force_download)
    binary, provenance = sqlite_tools.build(build_dir, "speedtest1", sources, args.force_download)
    sync = "off" if args.nosync else "full"
    case = (
        f"main/{args.jmode}/{sync}/S{args.size}/P{args.page_size}"
        f"/K{args.cache_size}/C1"
    )
    ctx.parameters.clear()
    ctx.parameters.update({
        "testset": "main",
        "size": args.size,
        "cache_size_kib": args.cache_size,
        "page_size": args.page_size,
        "jmode": args.jmode,
        "nosync": args.nosync,
        "sort_threads": 0,
        "mmap_bytes": 0,
        "repeats": args.repeats,
        "tool_versions": {"compiler": provenance["compiler_version"]},
        "source_sha256": provenance["source_sha256"],
        "binary_sha256": provenance["binary_sha256"],
    })
    for repeat in range(1, args.repeats + 1):
        # Each complete suite starts from a new database; caches are not evicted.
        directory = ctx.work_dir / f"repeat-{repeat}"
        directory.mkdir()
        command = [
            str(binary), "--testset", "main", "--size", str(args.size),
            "--journal", args.jmode, "--pagesize", str(args.page_size),
            "--cachesize", str(-args.cache_size), "--threads", "0",
            str(directory / "speedtest.sqlite"),
        ]
        if args.nosync:
            command.append("--nosync")
        started = time.perf_counter()
        process = sqlite_tools.run_command(command)
        wall_s = time.perf_counter() - started
        parsed = parse_speedtest_output(process.stdout)
        if parsed["sqlite_version"] != sqlite_tools.SQLITE_VERSION:
            raise sqlite_tools.BenchmarkError("speedtest1 reported a different SQLite version")
        ctx.parameters["tool_versions"]["sqlite"] = parsed["sqlite_version"]
        for stage in parsed["stages"]:
            ctx.emit(
                f"{case}/test-{stage['id']}", {"elapsed_s": stage["elapsed_s"]},
                repeat=repeat, details={"description": stage["description"]},
            )
        ctx.emit(
            f"{case}/total",
            {"official_total_s": parsed["official_total_s"], "wall_s": wall_s},
            repeat=repeat,
        )
