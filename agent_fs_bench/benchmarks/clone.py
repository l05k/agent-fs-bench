"""Durable same-filesystem directory clones, including concurrent full-tree requests.

Only --smoke is exposed on the workload CLI. Edit CONFIG for a deliberate
experiment; targets are mounted externally. Every request runs one command,
fsyncs its entire destination tree, then fsyncs the prepared destination parent.
Source generation, per-repeat preread, worker startup and verification are not
timed. A successful fsync is the experiment's persistence boundary.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Sequence

from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import RunContext

MIB = 1024 * 1024
GIB = 1024 * MIB
CHUNK = 4 * MIB

LATENCY = dict(series=("target", "copier"), logx=True, logy=True,
               xlabel="Concurrency", ylabel="Clone latency (s)")
FIGURES = [
    Figure("requests", "total_bytes", "latency_s", where={"experiment": "bytes"},
           series=("target", "copier"), xscale=MIB, logx=True, logy=True,
           xlabel="Total bytes (MiB)", ylabel="Clone latency (s)", name="latency-bytes"),
    Figure("requests", "files", "latency_s", where={"experiment": "files"},
           series=("target", "copier"), logx=True, logy=True,
           xlabel="Total files", ylabel="Clone latency (s)", name="latency-files"),
    Figure("requests", "concurrency", "latency_s",
           where={"experiment": "concurrency", "kind": "large"}, name="latency-concurrency-large",
           **LATENCY),
    Figure("requests", "concurrency", "latency_s",
           where={"experiment": "concurrency", "kind": "small"}, name="latency-concurrency-small",
           **LATENCY),
    Figure("batches", "concurrency", "clones_s",
           where={"experiment": "concurrency", "kind": "large"}, series=("target", "copier"),
           logx=True, xlabel="Concurrency", ylabel="Throughput (clones/s)",
           name="throughput-large"),
    Figure("batches", "concurrency", "clones_s",
           where={"experiment": "concurrency", "kind": "small"}, series=("target", "copier"),
           logx=True, xlabel="Concurrency", ylabel="Throughput (clones/s)",
           name="throughput-small"),
]

# Commands run without a shell. Only selected commands must be installed.
# Example: target_copiers={"LakeBase": ["lakebase", "cp-r"]}; other targets
# retain the default cp-r. Target names are the shared runner's explicit aliases.
CONFIG: dict[str, Any] = {
    "stage": "all",
    "repeats": 5,
    "copiers": ["cp-r"],
    "target_copiers": {},
    "commands": {
        "cp-r": "cp -r {src} {dst}",
        "lakebase": "lakebase-client clone {src} {dst}",
    },
    "byte_totals": (16 * MIB, 64 * MIB, 256 * MIB, GIB, 4 * GIB),
    "byte_files": 16,
    "file_counts": (100, 1000, 10000, 100000),
    "small_file_size": 4096,
    "conc_levels": (1, 2, 4, 8, 16),
    "concurrency_large_bytes": 256 * MIB,
    "concurrency_small_files": 10000,
    "files_per_dir": 128,
    "keep_scratch": False,
}


class BenchmarkError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workload:
    experiment: str
    kind: str
    files: int
    file_size: int
    concurrency: int = 1

    @property
    def total_bytes(self) -> int:
        return self.files * self.file_size


@dataclass(frozen=True)
class Copier:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class RequestResult:
    copy_s: float
    fsync_s: float
    latency_s: float
    completed_at: float


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def fstype(path: Path) -> str:
    """Read the most specific Linux mount type; FUSE type is not a vendor ID."""
    path_string = str(path.resolve())
    best_mount, best_type = "", "unknown"
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            before, _, after = line.partition(" - ")
            mount = re.sub(
                r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)),
                before.split()[4],
            ).rstrip("/") or "/"
            if path_string == mount or path_string.startswith(mount.rstrip("/") + "/"):
                if len(mount) >= len(best_mount):
                    best_mount, best_type = mount, after.split()[0]
    return best_type


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def raise_walk_error(error: OSError) -> None:
    raise error


def fsync_tree(path: Path) -> None:
    """Sync file contents and directory entries from leaves toward the root."""
    for directory, _dirs, files in os.walk(path, topdown=False, onerror=raise_walk_error):
        root = Path(directory)
        for name in files:
            fsync_file(root / name)
        fsync_dir(root)


def gen_dataset(spec: Workload, source: Path, files_per_dir: int) -> None:
    source.mkdir(parents=True)
    (source / "empty").mkdir()
    for index in range(spec.files):
        directory = source / f"d_{index // files_per_dir}"
        directory.mkdir(exist_ok=True)
        with (directory / f"f_{index}.bin").open("wb") as handle:
            remaining = spec.file_size
            while remaining:
                block = os.urandom(min(CHUNK, remaining))
                handle.write(block)
                remaining -= len(block)
    fsync_tree(source)
    fsync_dir(source.parent)


def tree_inventory(root: Path) -> dict[str, int | None]:
    inventory: dict[str, int | None] = {}
    for directory, dirs, files in os.walk(root, onerror=raise_walk_error):
        path = Path(directory)
        inventory[str(path.relative_to(root))] = None
        for name in dirs:
            inventory[str((path / name).relative_to(root))] = None
        for name in files:
            inventory[str((path / name).relative_to(root))] = (path / name).stat().st_size
    return inventory


def verify_tree(
    source: Path,
    destination: Path,
    expected: dict[str, int | None] | None = None,
    *,
    contents: bool = False,
) -> None:
    expected = tree_inventory(source) if expected is None else expected
    if tree_inventory(destination) != expected:
        raise BenchmarkError(f"tree structure or sizes differ: {destination}")
    if contents:
        for relative, size in expected.items():
            if size is not None:
                with (source / relative).open("rb") as original, (destination / relative).open("rb") as copied:
                    if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(copied, "sha256").digest():
                        raise BenchmarkError(f"file contents differ: {destination / relative}")


def warm_source(source: Path, expected_bytes: int) -> None:
    """Preread before each measurement; neither pins pages nor resets remote caches."""
    read_bytes = 0
    for directory, _dirs, files in os.walk(source, onerror=raise_walk_error):
        for name in files:
            with (Path(directory) / name).open("rb") as handle:
                while block := handle.read(CHUNK):
                    read_bytes += len(block)
    if read_bytes != expected_bytes:
        raise BenchmarkError(f"source preread: {read_bytes} bytes, expected {expected_bytes}")


def resolve_copiers(args: argparse.Namespace, target_name: str) -> list[Copier]:
    """Expand the selected copier names into executable argv templates."""
    names = args.target_copiers.get(target_name, args.copiers)
    if not names:
        raise BenchmarkError(f"no copiers configured for {target_name}")
    copiers = []
    for name in dict.fromkeys(names):
        if name not in args.commands:
            raise BenchmarkError(f"no command configured for copier {name!r}")
        argv = shlex.split(args.commands[name])
        executable = shutil.which(argv[0]) if argv else None
        if executable is None:
            raise BenchmarkError(f"{name}: executable not found: {argv[0] if argv else ''}")
        copiers.append(Copier(name, (executable, *argv[1:])))
    return copiers


def copy_request(source: Path, destination: Path, copier: Copier) -> RequestResult:
    """Copy one complete tree into an absent destination, including persistence."""
    command = [token.replace("{src}", str(source)).replace("{dst}", str(destination)) for token in copier.argv]
    started = time.perf_counter()
    subprocess.run(command, check=True)
    copied_at = time.perf_counter()
    fsync_tree(destination)
    fsync_dir(destination.parent)
    completed_at = time.perf_counter()
    return RequestResult(copied_at - started, completed_at - copied_at, completed_at - started, completed_at)


def copy_requests(
    source: Path, destinations: Sequence[Path], copier: Copier,
) -> tuple[float, list[RequestResult]]:
    """Start full-tree requests together; exclude thread startup and shutdown."""
    if len(destinations) == 1:
        started = time.perf_counter()
        result = copy_request(source, destinations[0], copier)
        return result.completed_at - started, [result]

    started = 0.0

    def start_clock() -> None:
        nonlocal started
        started = time.perf_counter()

    barrier = threading.Barrier(len(destinations) + 1, action=start_clock)

    def worker(destination: Path) -> RequestResult:
        barrier.wait()
        return copy_request(source, destination, copier)

    with cf.ThreadPoolExecutor(max_workers=len(destinations)) as executor:
        futures = [executor.submit(worker, destination) for destination in destinations]
        barrier.wait()
        results = [future.result() for future in futures]
    return max(result.completed_at for result in results) - started, results


def prepare_destinations(root: Path, concurrency: int) -> list[Path]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    destinations = []
    for index in range(concurrency):
        parent = root / f"request_{index}"
        parent.mkdir()
        fsync_dir(parent)
        destinations.append(parent / "tree")
    fsync_dir(root)
    fsync_dir(root.parent)
    return destinations


def validate_copier(copier: Copier, scratch: Path) -> None:
    """Check content, empty directories and bidirectional write isolation once."""
    root = scratch / f"validation-{copier.name}"
    source, destination = root / "source", root / "destination"
    try:
        (source / "nested").mkdir(parents=True)
        (source / "empty").mkdir()
        original = bytes(range(256)) * 8
        source_file = source / "nested" / "payload"
        source_file.write_bytes(original)
        fsync_tree(source)
        fsync_dir(root)
        fsync_dir(root.parent)
        copy_request(source, destination, copier)
        verify_tree(source, destination, contents=True)
        destination_file = destination / "nested" / "payload"
        with destination_file.open("r+b") as handle:
            handle.write(b"destination mutation")
        if source_file.read_bytes() != original:
            raise BenchmarkError(f"{copier.name}: destination write changed source")
        changed_destination = destination_file.read_bytes()
        with source_file.open("r+b") as handle:
            handle.write(b"source mutation")
        if destination_file.read_bytes() != changed_destination:
            raise BenchmarkError(f"{copier.name}: source write changed destination")
    finally:
        shutil.rmtree(root)


def build_plan(args: argparse.Namespace) -> list[Workload]:
    plan = []
    if args.stage in ("bytes", "all"):
        for total in args.byte_totals:
            if total % args.byte_files:
                raise BenchmarkError("byte_totals must be divisible by byte_files")
            plan.append(Workload("bytes", "large", args.byte_files, total // args.byte_files))
    if args.stage in ("files", "all"):
        plan.extend(Workload("files", "small", count, args.small_file_size) for count in args.file_counts)
    if args.stage in ("concurrency", "all"):
        if args.concurrency_large_bytes % args.byte_files:
            raise BenchmarkError("concurrency_large_bytes must be divisible by byte_files")
        for concurrency in args.conc_levels:
            plan.extend((
                Workload("concurrency", "large", args.byte_files,
                         args.concurrency_large_bytes // args.byte_files, concurrency),
                Workload("concurrency", "small", args.concurrency_small_files,
                         args.small_file_size, concurrency),
            ))
    return plan


def run_workload(ctx: RunContext, spec: Workload, args: argparse.Namespace, copiers: Sequence[Copier]) -> None:
    scratch = ctx.work_dir / "scratch"
    label = f"{spec.experiment}-{spec.kind}-{spec.files}-{spec.file_size}-{spec.concurrency}"
    root = scratch / label
    source, destination_root = root / "source", root / "destinations"
    copies = spec.concurrency + 1
    entries = spec.files + (spec.files + args.files_per_dir - 1) // args.files_per_dir + 2
    need = copies * (spec.total_bytes + entries * 4096) + 256 * MIB
    if shutil.disk_usage(ctx.target.path).free < need:
        raise BenchmarkError(f"{label}: needs approximately {need / MIB:.0f} MiB free")
    root.mkdir()
    fsync_dir(scratch)
    try:
        log(f"[{ctx.target.name}] generate {label}: {spec.total_bytes / MIB:g} MiB per request")
        gen_dataset(spec, source, args.files_per_dir)
        inventory = tree_inventory(source)
        for repeat in range(1, args.repeats + 1):
            # Rotate operations to avoid making one copier first on every repeat.
            offset = (repeat - 1) % len(copiers)
            for copier in (*copiers[offset:], *copiers[:offset]):
                destinations = prepare_destinations(destination_root, spec.concurrency)
                warm_source(source, spec.total_bytes)
                wall, results = copy_requests(source, destinations, copier)
                for destination in destinations:
                    verify_tree(source, destination, inventory)
                keys = {
                    "copier": copier.name,
                    "experiment": spec.experiment,
                    "kind": spec.kind,
                    "files": spec.files,
                    "file_size_bytes": spec.file_size,
                    "total_bytes": spec.total_bytes,
                    "concurrency": spec.concurrency,
                    "repeat": repeat,
                }
                ctx.emit("batches", keys, {"wall_s": wall, "clones_s": spec.concurrency / wall})
                for sample, result in enumerate(results, 1):
                    ctx.emit("requests", {**keys, "sample": sample},
                             {"copy_s": result.copy_s, "fsync_s": result.fsync_s,
                              "latency_s": result.latency_s})
                log(f"[{ctx.target.name}/{copier.name}] {label} rep{repeat}: "
                    f"batch={wall:.4f}s, {spec.concurrency / wall:.2f} clones/s")
    finally:
        if not args.keep_scratch:
            shutil.rmtree(root)


def executable_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    copiers = resolve_copiers(args, ctx.target.name)
    plan = build_plan(args)
    ctx.info["fstype"] = fstype(ctx.target.path)
    ctx.info["copier_sha256"] = {copier.name: executable_sha256(copier.argv[0]) for copier in copiers}
    scratch = ctx.work_dir / "scratch"
    scratch.mkdir()
    # Persist the run namespace prepared by the shared runner before any timing.
    directory, target = scratch.resolve(), ctx.target.path.resolve()
    if not directory.is_relative_to(target):
        raise BenchmarkError("scratch directory escapes target")
    while directory != target:
        fsync_dir(directory)
        directory = directory.parent
    fsync_dir(target)
    for copier in copiers:
        validate_copier(copier, scratch)
    for spec in plan:
        run_workload(ctx, spec, args, copiers)
    if not args.keep_scratch:
        scratch.rmdir()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="run small real sweeps with two repeats")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    if parsed.smoke:
        settings.update(
            stage="all", repeats=2, byte_totals=(MIB, 4 * MIB), byte_files=4,
            file_counts=(8, 32), small_file_size=4096, conc_levels=(1, 4),
            concurrency_large_bytes=4 * MIB, concurrency_small_files=32,
        )
    args = argparse.Namespace(**settings, **vars(parsed))
    return args
