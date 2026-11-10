"""Durable same-filesystem directory clones at one external concurrency level.

One run measures one directory shape (`files` x `file_size_bytes`) at one `tasks`
value: every task issues one complete-tree clone request on its own machine, and
one global Open MPI job starts all requests together. Curves over tasks come from
several runs pooled with `plot --vary tasks`.

Only --smoke is exposed on the workload CLI. Edit CONFIG for a deliberate
experiment; targets are mounted externally. A run uses one copier per target. Every
request runs one command, fsyncs its entire destination tree, then fsyncs the
prepared destination parent. Per-node preparation, per-repeat preread, task startup
and verification are not timed. A successful fsync is the experiment's persistence
boundary.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Any

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import RunContext

MIB = 1024 * 1024
CHUNK = 4 * MIB

# A shape (files x file_size_bytes) is one run; pooled runs must not mix shapes.
SHAPE = ("files", "file_size_bytes")
LATENCY = dict(series=("target", "copier"), logx=True, logy=True,
               xlabel="Tasks", ylabel="Clone latency (s)")
THROUGHPUT = dict(series=("target", "copier"), logx=True,
                  xlabel="Tasks", ylabel="Throughput (clones/s)")
FIGURES = [
    Figure("requests", "tasks", "latency_s", split=SHAPE, name="latency-tasks", **LATENCY),
    Figure("batches", "tasks", "clones_s", split=SHAPE, name="throughput-tasks", **THROUGHPUT),
    Figure("requests", "total_bytes", "latency_s", split=("files", "tasks"),
           series=("target", "copier"), xscale=MIB, logx=True, logy=True,
           xlabel="Total bytes (MiB)", ylabel="Clone latency (s)", name="latency-bytes"),
    Figure("requests", "files", "latency_s", split=("file_size_bytes", "tasks"),
           series=("target", "copier"), logx=True, logy=True,
           xlabel="Total files", ylabel="Clone latency (s)", name="latency-files"),
]

# Commands run without a shell. Only the selected command must be installed.
# One copier per run: `copier`, or the target's entry in target_copiers, e.g.
# target_copiers={"LakeBase": "lakebase"}. Target names are the runner's aliases.
CONFIG: dict[str, Any] = {
    "repeats": 5,
    "copier": "cp-r",
    "target_copiers": {},
    "commands": {
        "cp-r": "cp -r {src} {dst}",
        "lakebase": "lakebase-client clone {src} {dst}",
    },
    "files": 10000,
    "file_size_bytes": 4096,
    "files_per_dir": 128,
    "tasks": 1,
    "cpus_per_task": 1,
    "oversubscribe": 1,
    "keep_scratch": False,
}

class BenchmarkError(RuntimeError):
    pass

@dataclass(frozen=True)
class Workload:
    """The one directory shape a run clones: identical for every task and repeat."""

    files: int
    file_size_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.files * self.file_size_bytes

@dataclass(frozen=True)
class Copier:
    name: str
    argv: tuple[str, ...]

@dataclass(frozen=True)
class RequestResult:
    copy_s: float
    fsync_s: float
    latency_s: float

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
            remaining = spec.file_size_bytes
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

def resolve_copier(args: argparse.Namespace, target_name: str) -> Copier:
    """The one copier this target uses, as an executable argv template."""
    name = args.target_copiers.get(target_name, args.copier)
    if not isinstance(name, str):
        raise BenchmarkError(f"copier for {target_name} must be one command name: {name!r}")
    if name not in args.commands:
        raise BenchmarkError(f"no command configured for copier {name!r}")
    argv = shlex.split(args.commands[name])
    executable = shutil.which(argv[0]) if argv else None
    if executable is None:
        raise BenchmarkError(f"{name}: executable not found: {argv[0] if argv else ''}")
    return Copier(name, (executable, *argv[1:]))

def copy_request(source: Path, destination: Path, copier: Copier) -> RequestResult:
    """Copy one complete tree into an absent destination, including persistence."""
    command = [token.replace("{src}", str(source)).replace("{dst}", str(destination)) for token in copier.argv]
    started = time.perf_counter()
    subprocess.run(command, check=True)
    copied_at = time.perf_counter()
    fsync_tree(destination)
    fsync_dir(destination.parent)
    completed_at = time.perf_counter()
    return RequestResult(copied_at - started, completed_at - copied_at, completed_at - started)

def prepare_destination(task_dir: Path) -> Path:
    """Replace this task's destination parent with a fresh one; return an absent tree."""
    root = task_dir / "destination"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    fsync_dir(root)
    fsync_dir(task_dir)
    return root / "tree"

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

def build_plan(args: argparse.Namespace) -> Workload:
    """Validate the one directory shape this run measures; `cluster.allocate` checks tasks."""
    for name in ("files", "file_size_bytes", "files_per_dir", "repeats"):
        value = getattr(args, name)
        if type(value) is not int or value < 1:
            raise BenchmarkError(f"{name} must be a positive integer: {value!r}")
    return Workload(args.files, args.file_size_bytes)

def executable_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()

def fsync_ancestors(directory: Path, target: Path) -> None:
    """Persist `directory` and every ancestor entry up to the target root."""
    directory, target = directory.resolve(), target.resolve()
    if not directory.is_relative_to(target):
        raise BenchmarkError(f"{directory} escapes target {target}")
    while directory != target:
        fsync_dir(directory)
        directory = directory.parent
    fsync_dir(target)

def _prepare_node(
    t: cluster.Task, args: argparse.Namespace, spec: Workload,
) -> tuple[Copier, dict[str, int | None]]:
    """One node's copier, source tree and facts; every task receives them."""
    copier: Copier | None = None
    inventory: dict[str, int | None] | None = None
    info: dict[str, Any] | None = None
    # Every task directory exists before the run tree is persisted.
    t.node_barrier()
    if t.leader:
        scratch = t.node_dir / "scratch"
        scratch.mkdir()
        copier = resolve_copier(args, t.target.name)
        validate_copier(copier, scratch)
        entries = spec.files + (spec.files + args.files_per_dir - 1) // args.files_per_dir + 2
        need = (t.local_count + 1) * (spec.total_bytes + entries * 4096) + 256 * MIB
        free = shutil.disk_usage(t.node_dir).free
        if free < need:
            raise BenchmarkError(f"{t.node}: needs approximately {need / MIB:.0f} MiB free, "
                                 f"{free / MIB:.0f} MiB available")
        t.log(f"[{t.target.name}/{t.node}] generate {spec.files} files x "
              f"{spec.file_size_bytes} B = {spec.total_bytes / MIB:g} MiB per request")
        gen_dataset(spec, t.node_dir / "source", args.files_per_dir)
        # The run tree created by the framework is durable before any timing.
        fsync_ancestors(t.node_dir, t.target.path)
        inventory = tree_inventory(t.node_dir / "source")
        info = {"fstype": fstype(t.target.path),
                "copier_sha256": {copier.name: executable_sha256(copier.argv[0])}}
    prepared = t.node_comm.bcast(
        {"copier": copier, "inventory": inventory} if t.leader else None, root=0)
    t.node_info(info)
    t.node_barrier()
    return prepared["copier"], prepared["inventory"]

def _cleanup(t: cluster.Task, keep_scratch: bool) -> None:
    if keep_scratch:
        return
    t.node_barrier()
    if t.leader:
        shutil.rmtree(t.node_dir)

def task(t: cluster.Task) -> None:
    args = t.args
    spec = build_plan(args)
    source = t.node_dir / "source"
    copier, inventory = _prepare_node(t, args, spec)
    hosts = t.gather(t.node)
    for repeat in range(1, args.repeats + 1):
        destination = prepare_destination(t.task_dir)
        warm_source(source, spec.total_bytes)
        wall, results = t.batch(copy_request, source, destination, copier)
        verify_tree(source, destination, inventory)
        if t.index != 0:
            continue
        keys = {"copier": copier.name, "files": spec.files,
                "file_size_bytes": spec.file_size_bytes,
                "total_bytes": spec.total_bytes, "tasks": t.count, "repeat": repeat}
        t.emit("batches", keys, {"wall_s": wall, "clones_s": t.count / wall})
        for sample, result in enumerate(results):
            t.emit("requests", {**keys, "sample": sample, "host": hosts[sample]},
                   {"copy_s": result.copy_s, "fsync_s": result.fsync_s,
                    "latency_s": result.latency_s})
        t.log(f"[{t.target.name}/{copier.name}] {spec.files}x{spec.file_size_bytes} "
              f"tasks={t.count} rep{repeat}: batch={wall:.4f}s, "
              f"{t.count / wall:.2f} clones/s")
    _cleanup(t, args.keep_scratch)

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    build_plan(args)
    cluster.run_tasks(ctx, args)

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="copy 32 files of 4096 B with two repeats")
    parsed = parser.parse_args(argv)
    settings = dict(CONFIG)
    if parsed.smoke:
        settings.update(files=32, file_size_bytes=4096, repeats=2)
    return argparse.Namespace(**settings, **vars(parsed))
