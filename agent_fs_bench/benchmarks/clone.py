"""Durable same-filesystem directory clones at one external concurrency level.

One run measures one directory shape (`files` x `file_size_bytes`) at one `tasks`
value: every task issues one complete-tree clone request on its own machine, and
one global Open MPI job starts all requests together. Curves over tasks come from
several runs pooled with `plot --vary tasks`.

Only --smoke is exposed on the workload CLI. Edit CONFIG for a deliberate
experiment; targets are mounted externally. A run uses one copier per target:
GNU `cp -r --reflink=never` plus destination fsyncs, or the Alibaba Cloud PolarDB
OpenAPI `ClonePolarFsBasicSnapshot` call. The native call is synchronous; its
successful return is the persistence boundary, so it does not fsync. Per-node
preparation, per-repeat preread, task startup and verification are not timed.
File contents are pseudo-random bytes from `random.Random(seed)`, so every run
with the same config clones byte-identical inputs on every machine.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import random
import re
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

CP = "cp-r"
POLARFS = "polarfs"
COPIERS = (CP, POLARFS)
CP_ARGS = ("-r", "--reflink=never", "{src}", "{dst}")
POLARFS_REQUIRED = ("instance_id", "region_id", "endpoint")
POLARFS_OPTIONAL = ("volume_root",)
CREDENTIALS = ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
SECURITY_TOKEN = "ALIBABA_CLOUD_SECURITY_TOKEN"

# One copier per run: `copier`, or the target's entry in target_copiers, e.g.
# target_copiers={"LakeBase": "polarfs"}. Target names are the runner's aliases.
# Native targets need a non-secret `polarfs` entry; credentials come only from the
# standard ALIBABA_CLOUD_* environment variables. A full-volume mount maps to "/".
CONFIG: dict[str, Any] = {
    "repeats": 5,
    "copier": CP,
    "target_copiers": {},
    "polarfs": {},
    "files": 10000,
    "file_size_bytes": 4096,
    "files_per_dir": 128,
    "tasks": 1,
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
class PolarFs:
    """One explicit host-mount root to volume-absolute path mapping."""

    instance_id: str
    region_id: str
    endpoint: str
    volume_root: str
    mount: Path

    def volume_path(self, host_path: Path) -> str:
        mount = self.mount.resolve()
        host = host_path.resolve()
        try:
            relative = host.relative_to(mount)
        except ValueError:
            raise BenchmarkError(
                f"polarfs path {host}: expected under mounted volume {mount}") from None
        return str(PurePosixPath(self.volume_root, *relative.parts))

@dataclass(frozen=True)
class Copier:
    name: str
    argv: tuple[str, ...] = ()
    polarfs: PolarFs | None = None

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

def gen_dataset(spec: Workload, source: Path, files_per_dir: int, seed: int) -> None:
    """Incompressible but reproducible contents: one seeded stream over all files in order."""
    stream = random.Random(seed)
    source.mkdir(parents=True)
    (source / "empty").mkdir()
    for index in range(spec.files):
        directory = source / f"d_{index // files_per_dir}"
        directory.mkdir(exist_ok=True)
        with (directory / f"f_{index}.bin").open("wb") as handle:
            remaining = spec.file_size_bytes
            while remaining:
                block = stream.randbytes(min(CHUNK, remaining))
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

def polarfs_settings(settings: Any) -> tuple[str, str, str, str]:
    """Validate one target's non-secret native settings; return normalized values."""
    if not isinstance(settings, dict):
        raise BenchmarkError(
            f"polarfs settings: expected a table, measured {type(settings).__name__}")
    unknown = set(settings) - set(POLARFS_REQUIRED) - set(POLARFS_OPTIONAL)
    if unknown:
        raise BenchmarkError(f"polarfs settings: unknown keys {sorted(unknown)}")
    missing = [key for key in POLARFS_REQUIRED if key not in settings]
    if missing:
        raise BenchmarkError(f"polarfs settings: missing {', '.join(missing)}")
    values: list[str] = []
    for key in POLARFS_REQUIRED:
        value = settings[key]
        if not isinstance(value, str) or not value.strip():
            raise BenchmarkError(
                f"polarfs {key}: expected a non-empty string, measured {value!r}")
        values.append(value)
    volume_root = settings.get("volume_root", "/")
    if not isinstance(volume_root, str) or not volume_root.strip():
        raise BenchmarkError(
            f"polarfs volume_root: expected a non-empty string, measured {volume_root!r}")
    volume = PurePosixPath(volume_root)
    if not volume.is_absolute() or ".." in volume.parts:
        raise BenchmarkError(
            "polarfs volume_root: expected an absolute volume path without '..', "
            f"measured {volume_root!r}")
    return values[0], values[1], values[2], str(volume)

def check_credentials() -> None:
    """Fail unless the standard SDK credential environment is present."""
    missing = [name for name in CREDENTIALS if not os.environ.get(name)]
    if missing:
        raise BenchmarkError(
            f"polarfs: missing environment variable(s) {', '.join(missing)}")

def resolve_copier(
    args: argparse.Namespace, target_name: str, target_path: Path,
) -> Copier:
    """The one copier this target uses; native settings are non-secret."""
    name = args.target_copiers.get(target_name, args.copier)
    if name not in COPIERS:
        raise BenchmarkError(
            f"copier for {target_name}: expected one of {COPIERS}, measured {name!r}")
    if name == CP:
        executable = shutil.which("cp")
        if not executable:
            raise BenchmarkError("cp: expected GNU coreutils on PATH")
        version = subprocess.run([executable, "--version"], capture_output=True, text=True)
        if version.returncode or "GNU coreutils" not in version.stdout:
            raise BenchmarkError(
                f"cp: expected GNU coreutils with --reflink, measured {executable!r}")
        return Copier(CP, (executable, *CP_ARGS))
    settings = args.polarfs.get(target_name)
    if settings is None:
        raise BenchmarkError(
            f"polarfs settings for {target_name}: expected a table in CONFIG['polarfs']")
    instance_id, region_id, endpoint, volume_root = polarfs_settings(settings)
    check_credentials()
    return Copier(POLARFS, polarfs=PolarFs(
        instance_id, region_id, endpoint, volume_root, target_path.resolve()))

def polarfs_sdk_version() -> str:
    from alibabacloud_polardb20170801 import __version__
    return __version__

def polarfs_client(polarfs: PolarFs):
    """Official PolarDB SDK client; credentials stay in this process environment."""
    check_credentials()
    from alibabacloud_polardb20170801.client import Client
    from alibabacloud_tea_openapi import models as open_api_models
    from darabonba.policy.retry import RetryOptions
    return Client(open_api_models.Config(
        access_key_id=os.environ[CREDENTIALS[0]],
        access_key_secret=os.environ[CREDENTIALS[1]],
        security_token=os.environ.get(SECURITY_TOKEN) or None,
        region_id=polarfs.region_id,
        endpoint=polarfs.endpoint,
        retry_options=RetryOptions(retryable=False),
    ))

def polarfs_clone(source: Path, destination: Path, polarfs: PolarFs) -> RequestResult:
    """One synchronous native clone; SDK success is the durability boundary."""
    source_volume = polarfs.volume_path(source)
    target_volume = polarfs.volume_path(destination)
    if not source.is_dir():
        raise BenchmarkError(f"polarfs source: expected a directory, measured {source}")
    if destination.exists():
        raise BenchmarkError(f"polarfs target: expected absent, measured {destination}")
    if not destination.parent.is_dir():
        raise BenchmarkError(
            f"polarfs target parent: expected a directory, measured {destination.parent}")
    client = polarfs_client(polarfs)
    from alibabacloud_polardb20170801 import models as polardb_models
    request = polardb_models.ClonePolarFsBasicSnapshotRequest(
        polar_fs_instance_id=polarfs.instance_id,
        source_path=source_volume,
        target_path=target_volume,
    )
    started = time.perf_counter()
    try:
        response = client.clone_polar_fs_basic_snapshot(request)
    except Exception as error:
        raise BenchmarkError(
            f"polarfs clone {source_volume} -> {target_volume}: {error}") from error
    completed_at = time.perf_counter()
    if response.status_code != 200 or response.body is None:
        raise BenchmarkError(
            f"polarfs clone {source_volume} -> {target_volume}: "
            f"status {response.status_code}, body {response.body!r}")
    elapsed = completed_at - started
    return RequestResult(elapsed, 0.0, elapsed)

def copy_request(source: Path, destination: Path, copier: Copier) -> RequestResult:
    """Copy one complete tree into an absent destination, including persistence."""
    if copier.polarfs is not None:
        return polarfs_clone(source, destination, copier.polarfs)
    command = [token.replace("{src}", str(source)).replace("{dst}", str(destination))
               for token in copier.argv]
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
    """Validate the one shape and copier configuration this run measures."""
    for name in ("files", "file_size_bytes", "files_per_dir", "repeats"):
        value = getattr(args, name)
        if type(value) is not int or value < 1:
            raise BenchmarkError(f"{name} must be a positive integer: {value!r}")
    if args.copier not in COPIERS:
        raise BenchmarkError(f"copier: expected one of {COPIERS}, measured {args.copier!r}")
    for target_name, name in args.target_copiers.items():
        if name not in COPIERS:
            raise BenchmarkError(
                f"copier for {target_name}: expected one of {COPIERS}, measured {name!r}")
    for settings in args.polarfs.values():
        polarfs_settings(settings)
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
        copier = resolve_copier(args, t.target.name, t.target.path)
        validate_copier(copier, scratch)
        entries = spec.files + (spec.files + args.files_per_dir - 1) // args.files_per_dir + 2
        copies = 1 if copier.polarfs is not None else t.local_count + 1
        need = copies * (spec.total_bytes + entries * 4096) + 256 * MIB
        free = shutil.disk_usage(t.node_dir).free
        if free < need:
            raise BenchmarkError(f"{t.node}: needs approximately {need / MIB:.0f} MiB free, "
                                 f"{free / MIB:.0f} MiB available")
        t.log(f"[{t.target.name}/{t.node}] generate {spec.files} files x "
              f"{spec.file_size_bytes} B = {spec.total_bytes / MIB:g} MiB per request")
        gen_dataset(spec, t.node_dir / "source", args.files_per_dir, args.seed)
        # The run tree created by the framework is durable before any timing.
        fsync_ancestors(t.node_dir, t.target.path)
        inventory = tree_inventory(t.node_dir / "source")
        info = {"fstype": fstype(t.target.path), "copier": copier.name}
        if copier.polarfs is None:
            info["copier_sha256"] = {copier.name: executable_sha256(copier.argv[0])}
        else:
            info["polarfs_sdk_version"] = polarfs_sdk_version()
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
