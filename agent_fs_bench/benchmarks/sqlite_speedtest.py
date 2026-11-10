"""Single-connection SQLite speedtest1 workloads on a mounted filesystem.

Every task runs one complete official main suite in its own database, so a run
with ``tasks`` tasks measures that many independent single-connection clients.
Repeats are aligned across tasks by MPI barriers and use a fresh per-task
directory. The node leader on each machine downloads or verifies the pinned
sources and builds ``speedtest1`` once, before any task touches the target; a
machine without network access needs the sources pre-placed in its build
directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import RunContext

from . import sqlite_tools

CONFIG = {
    "size": 100,
    "cache_size": 2000,  # KiB; passed as a negative PRAGMA cache_size.
    "page_size": 4096,
    "jmode": "delete",
    "nosync": False,
    "repeats": 5,
    "tasks": 1,
    "cpus_per_task": 1,
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

FIGURES = [
    Figure(
        "totals", "tasks", "official_total_s", kind="box",
        split=("jmode",), xlabel="Tasks", ylabel="Speedtest1 total time (s)",
        name="speedtest-total",
    ),
    Figure(
        "stages", "stage", "elapsed_s",
        split=("tasks",), xlabel="Stage ID", ylabel="Stage elapsed time (s)",
        name="speedtest-stages",
    ),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="two tasks, two repeats and scale 1; needs two logical CPUs",
    )
    options = parser.parse_args(argv)
    values = dict(CONFIG)
    if options.smoke:
        values.update(tasks=2, repeats=2, size=1)
    args = argparse.Namespace(**values, smoke=options.smoke)
    args.jmode = args.jmode.lower()
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


def speedtest_argv(binary: Path, database: Path, args: argparse.Namespace) -> list[str]:
    argv = [
        str(binary), "--testset", "main", "--size", str(args.size),
        "--journal", args.jmode, "--pagesize", str(args.page_size),
        "--cachesize", str(-args.cache_size), "--threads", "0",
        str(database),
    ]
    if args.nosync:
        argv.append("--nosync")
    return argv


def _speedtest1(node: str, argv: list[str]) -> tuple[str, float, str]:
    started = time.perf_counter()
    process = sqlite_tools.run_command(argv)
    return node, time.perf_counter() - started, process.stdout


def task(t: cluster.Task) -> None:
    build_dir = sqlite_tools.build_directory(t.args.build_dir, "sqlite-speedtest")
    build_info = None
    if t.leader:
        sources = sqlite_tools.ensure_sources(
            build_dir, "speedtest1", t.args.force_download
        )
        _, provenance = sqlite_tools.build(
            build_dir, "speedtest1", sources, t.args.force_download
        )
        build_info = {
            "compiler": provenance["compiler_version"],
            "source_sha256": provenance["source_sha256"],
            "binary_sha256": provenance["binary_sha256"],
            "sqlite_version": sqlite_tools.SQLITE_VERSION,
        }
    t.node_barrier()
    t.node_info(build_info)

    binary = build_dir / "speedtest1"
    keys = {
        "jmode": t.args.jmode,
        "sync": "off" if t.args.nosync else "full",
        "size": t.args.size,
        "page_size": t.args.page_size,
        "cache_size_kib": t.args.cache_size,
    }
    for repeat in range(1, t.args.repeats + 1):
        # Each complete suite starts from a new database; caches are not evicted.
        directory = t.task_dir / f"repeat-{repeat}"
        directory.mkdir()
        _, results = t.batch(
            _speedtest1, t.node,
            speedtest_argv(binary, directory / "speedtest.sqlite", t.args),
        )
        if t.index != 0:
            continue
        for sample, (node, wall_s, stdout) in enumerate(results):
            parsed = parse_speedtest_output(stdout)
            if parsed["sqlite_version"] != sqlite_tools.SQLITE_VERSION:
                raise sqlite_tools.BenchmarkError(
                    f"task {sample} on {node} reported SQLite "
                    f"{parsed['sqlite_version']}, expected {sqlite_tools.SQLITE_VERSION}"
                )
            for stage in parsed["stages"]:
                t.emit(
                    "stages",
                    {
                        **keys,
                        "stage": stage["id"],
                        "description": stage["description"],
                        "tasks": t.count,
                        "repeat": repeat,
                        "sample": sample,
                        "host": node,
                    },
                    {"elapsed_s": stage["elapsed_s"]},
                )
            t.emit(
                "totals",
                {
                    **keys,
                    "tasks": t.count,
                    "repeat": repeat,
                    "sample": sample,
                    "host": node,
                },
                {"official_total_s": parsed["official_total_s"], "wall_s": wall_s},
            )
    if t.index == 0:
        t.info({"sqlite_version": sqlite_tools.SQLITE_VERSION})


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    cluster.run_tasks(ctx, args)
