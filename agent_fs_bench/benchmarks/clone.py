"""Same-filesystem copy/clone benchmark for local and network filesystems.

Each case generates a source tree on the filesystem under test, pre-reads it
to warm the page cache, then copies it to a fresh destination for every
repeat. There is deliberately no cold-cache mode: /proc/sys/vm/drop_caches
does not evict FUSE or other userspace client caches, so calling the result
"cold" would be misleading on exactly the distributed filesystems this tool
is meant to measure.

Built-in copiers:

  cp        cp -a (preserve attributes)
  cpr       cp -r (do not preserve attributes)
  internal  chunked Python read/write, useful as a data-movement baseline

Any external copier can be added through the ``CONFIG`` mapping below: each
entry is ``NAME=COMMAND``, the command is split with shell-like quoting and
must contain ``{src}`` and ``{dst}``; ``{threads}`` is also available. The
harness runs it once per shard.

The public CLI exposes only ``--smoke``. Every other experiment setting
(stage, repeats, copiers, fsync, dataset shape, scratch layout) lives in
``CONFIG`` and is meant to be edited for a deliberate experiment setup.

The measured workload uses only the Python standard library; the shared runner
needs DuckDB for its default post-run CSV summary. Linux is the intended runtime.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


from agent_fs_bench.results import RunContext

MIB = 1024 * 1024
GIB = 1024 * MIB
CHUNK = 4 * MIB
CACHE_STATE = "warm-source-preread"
BUILTIN_COPIER_NAMES = ("cp", "cpr", "internal")

# ---------------------------------------------------------------------------
# CONFIG: all experiment settings for this workload.
#
# The public CLI accepts only --smoke; change these values (in code) to run a
# different experiment setup. ``copiers`` is an ordered list of built-in names
# or names declared in ``external_copier`` (``NAME=COMMAND`` entries).
# ``kinds`` empty means every workload kind; otherwise list a subset of
# ``("large", "small")``.
# ---------------------------------------------------------------------------
CONFIG: dict[str, Any] = {
    "stage": "single",
    "repeats": 3,
    "copiers": ["cp"],
    "external_copier": [],
    "threads": 1,
    "cp_binary": "",
    "fsync": True,
    "conc_levels": (1, 4, 16),
    "small_count": 10000,
    "data_pattern": "random",
    "kinds": (),
    "scratch_subpath": "scratch",
    "keep_scratch": False,
}


class BenchmarkError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)




def _mountinfo_unescape(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _fstype_from_mountinfo(path: str) -> str | None:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            entries = fh.readlines()
    except OSError:
        return None
    best_mount = ""
    best_type = None
    for line in entries:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        if len(fields) < 5:
            continue
        mount_point = _mountinfo_unescape(fields[4]).rstrip("/") or "/"
        fs_type = after.split()[0] if after.split() else ""
        if not fs_type:
            continue
        if path == mount_point or path.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) >= len(best_mount):
                best_mount = mount_point
                best_type = fs_type
    return best_type


def fstype(path: str) -> str:
    """Return the filesystem type, or ``unknown`` when no probe works."""
    path = os.path.realpath(os.path.abspath(path))
    proc_type = _fstype_from_mountinfo(path)
    if proc_type:
        return proc_type
    findmnt = shutil.which("findmnt")
    if findmnt:
        try:
            return subprocess.check_output(
                [findmnt, "-no", "FSTYPE", "-T", path],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip() or "unknown"
        except (OSError, subprocess.CalledProcessError):
            pass
    stat = shutil.which("stat")
    if stat:
        try:
            return subprocess.check_output(
                [stat, "-f", "-c", "%T", path],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip() or "unknown"
        except (OSError, subprocess.CalledProcessError):
            pass
    return "unknown"


def fsync_dir(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path: str) -> None:
    # fsync through a read-only fd flushes the inode's dirty pages on Linux.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def raise_walk_error(error: OSError) -> None:
    raise error


def fsync_tree(path: str) -> None:
    for root, _dirs, files in os.walk(path, onerror=raise_walk_error):
        for name in files:
            fsync_file(os.path.join(root, name))
        fsync_dir(root)


# ----------------------------- source data ---------------------------------


def gen_large_file(path: str, size: int, data_pattern: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    zeros = b"\0" * CHUNK
    with open(path, "wb") as fh:
        left = size
        while left > 0:
            count = min(CHUNK, left)
            if data_pattern == "zeros":
                block = zeros if count == CHUNK else zeros[:count]
            else:
                block = os.urandom(count)
            fh.write(block)
            left -= count
        fh.flush()
        os.fsync(fh.fileno())


def gen_dataset(
    spec: dict[str, Any], src_root: str, data_pattern: str
) -> list[str]:
    """Build source shards under ``src_root`` and return their directories."""
    shard_count = spec.get("shards", spec["concurrency"])
    if spec["kind"] == "large":
        total = spec["total_bytes"]
        base, remainder = divmod(total, shard_count)
        shards = []
        for index in range(shard_count):
            size = base + (remainder if index == shard_count - 1 else 0)
            directory = os.path.join(src_root, f"shard_{index}")
            os.makedirs(directory, exist_ok=True)
            gen_large_file(
                os.path.join(directory, "file.bin"), size, data_pattern
            )
            shards.append(directory)
    else:
        count = spec["count"]
        file_size = spec["file_size"]
        shards = [
            os.path.join(src_root, f"shard_{index}")
            for index in range(shard_count)
        ]
        for directory in shards:
            os.makedirs(directory, exist_ok=True)
        for index in range(count):
            directory = shards[index % shard_count]
            path = os.path.join(directory, f"f_{index}.bin")
            blob = (
                b"\0" * file_size
                if data_pattern == "zeros"
                else os.urandom(file_size)
            )
            with open(path, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        for directory in shards:
            fsync_dir(directory)
    fsync_dir(src_root)
    return shards


def tree_inventory(root: str) -> dict[str, int]:
    inventory: dict[str, int] = {}
    for directory, _dirs, files in os.walk(root, onerror=raise_walk_error):
        for name in files:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root)
            inventory[rel] = os.path.getsize(path)
    return inventory


def dataset_bytes_files(shards: Iterable[str]) -> tuple[int, int]:
    total_bytes = 0
    total_files = 0
    for shard in shards:
        for directory, _dirs, files in os.walk(shard, onerror=raise_walk_error):
            for name in files:
                total_files += 1
                total_bytes += os.path.getsize(os.path.join(directory, name))
    return total_bytes, total_files


def warm_source(shards: Iterable[str], expected_bytes: int) -> None:
    """Best-effort page-cache warm-up; this does not pin pages in memory."""
    read_bytes = 0
    for shard in shards:
        for directory, _dirs, files in os.walk(shard, onerror=raise_walk_error):
            for name in files:
                path = os.path.join(directory, name)
                with open(path, "rb") as fh:
                    while True:
                        block = fh.read(CHUNK)
                        if not block:
                            break
                        read_bytes += len(block)
    if read_bytes != expected_bytes:
        raise BenchmarkError(
            f"source warm-up read {read_bytes} bytes, expected {expected_bytes}"
        )


def verify_tree(
    source: str, destination: str, expected: dict[str, int] | None = None
) -> None:
    expected = tree_inventory(source) if expected is None else expected
    actual = tree_inventory(destination)
    if actual == expected:
        return
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    wrong_size = sorted(
        name
        for name in set(expected) & set(actual)
        if expected[name] != actual[name]
    )
    details = []
    if missing:
        details.append(f"missing={missing[:3]}")
    if extra:
        details.append(f"extra={extra[:3]}")
    if wrong_size:
        details.append(f"wrong-size={wrong_size[:3]}")
    raise BenchmarkError(
        f"copy verification failed for {destination}: " + ", ".join(details)
    )


# ------------------------------ copiers ------------------------------------


@dataclass(frozen=True)
class Copier:
    name: str
    kind: str
    argv: tuple[str, ...] = ()


def internal_copy_tree(source: str, destination: str) -> None:
    os.makedirs(destination, exist_ok=True)
    for directory, _dirs, files in os.walk(source, onerror=raise_walk_error):
        rel = os.path.relpath(directory, source)
        target_dir = destination if rel == "." else os.path.join(destination, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            source_path = os.path.join(directory, name)
            destination_path = os.path.join(target_dir, name)
            with open(source_path, "rb") as source_fh, open(
                destination_path, "wb"
            ) as destination_fh:
                shutil.copyfileobj(source_fh, destination_fh, CHUNK)
                destination_fh.flush()
            shutil.copystat(source_path, destination_path)


def parse_external_copier(value: str) -> Copier:
    name, separator, command = value.partition("=")
    name = name.strip()
    if not separator or not name or not command.strip():
        raise BenchmarkError(
            "external_copier entries must look like NAME='command {src} {dst}'"
        )
    if name in BUILTIN_COPIER_NAMES:
        raise BenchmarkError(f"external copier {name!r} conflicts with a built-in")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise BenchmarkError(f"invalid external copier name: {name!r}")
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        raise BenchmarkError(f"cannot parse {name} command: {exc}") from exc
    if not argv:
        raise BenchmarkError(f"external copier {name!r} has an empty command")
    placeholders = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", command))
    unknown = placeholders - {"src", "dst", "threads"}
    if unknown:
        raise BenchmarkError(
            f"external copier {name!r} has unknown placeholders: "
            + ", ".join(sorted(unknown))
        )
    missing = {"src", "dst"} - placeholders
    if missing:
        raise BenchmarkError(
            f"external copier {name!r} is missing placeholders: "
            + ", ".join(sorted(missing))
        )
    return Copier(name=name, kind="external", argv=argv)


def resolve_copiers(args: argparse.Namespace) -> list[Copier]:
    names: list[str] = list(args.copiers)
    if not names:
        names = ["cp"]

    external: dict[str, Copier] = {}
    for value in args.external_copier:
        copier = parse_external_copier(value)
        if copier.name in external:
            raise BenchmarkError(f"duplicate external copier: {copier.name}")
        external[copier.name] = copier

    result = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if name == "cp":
            result.append(Copier(name="cp", kind="cp"))
        elif name == "cpr":
            result.append(Copier(name="cpr", kind="cpr"))
        elif name == "internal":
            result.append(Copier(name="internal", kind="internal"))
        elif name in external:
            result.append(external[name])
        else:
            valid = ", ".join(list(BUILTIN_COPIER_NAMES) + sorted(external))
            raise BenchmarkError(f"unknown copier {name!r}; available: {valid}")
    return result


def render_external(
    argv: Sequence[str], source: str, destination: str, threads: int
) -> list[str]:
    replacements = {
        "{src}": source,
        "{dst}": destination,
        "{threads}": str(threads),
    }
    rendered = []
    for token in argv:
        for placeholder, value in replacements.items():
            token = token.replace(placeholder, value)
        rendered.append(token)
    return rendered


def run_command(command: Sequence[str], description: str) -> None:
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise BenchmarkError(
            f"{description} exited with status {exc.returncode}: "
            + shlex.join(command)
        ) from exc


def copy_shard(
    source: str,
    destination: str,
    copier: Copier,
    do_fsync: bool,
    threads: int,
    cp_binary: str,
) -> dict[str, float]:
    if os.path.exists(destination):
        shutil.rmtree(destination)
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

    started = time.perf_counter()
    if copier.kind in ("cp", "cpr"):
        flag = "-a" if copier.kind == "cp" else "-r"
        run_command(
            [cp_binary, flag, source, destination],
            f"{copier.name} copy",
        )
    elif copier.kind == "internal":
        internal_copy_tree(source, destination)
    else:
        run_command(
            render_external(copier.argv, source, destination, threads),
            f"{copier.name} copy",
        )
    copy_seconds = time.perf_counter() - started

    fsync_seconds = 0.0
    if do_fsync:
        fsync_started = time.perf_counter()
        fsync_tree(destination)
        fsync_seconds = time.perf_counter() - fsync_started

    return {
        "copy_s": copy_seconds,
        "fsync_s": fsync_seconds,
        "wall_s": copy_seconds + fsync_seconds,
    }


def _copy_one(
    index: int,
    source: str,
    destination_root: str,
    copier: Copier,
    do_fsync: bool,
    threads: int,
    cp_binary: str,
) -> dict[str, float]:
    destination = os.path.join(destination_root, f"shard_{index}")
    return copy_shard(
        source, destination, copier, do_fsync, threads, cp_binary
    )


def copy_shards(
    shards: Sequence[str],
    destination_root: str,
    copier: Copier,
    do_fsync: bool,
    threads: int,
    cp_binary: str,
) -> tuple[float, float, float]:
    """Copy shards, returning ``(wall, max(copy), max(fsync))``.

    A one-shard case runs in this process. For multi-shard cases, wall time
    includes ProcessPoolExecutor startup; copy time does not. The report keeps
    those columns separate so the distinction is visible.
    """
    if len(shards) == 1:
        started = time.perf_counter()
        result = _copy_one(
            0, shards[0], destination_root, copier, do_fsync, threads, cp_binary
        )
        return (
            time.perf_counter() - started,
            result["copy_s"],
            result["fsync_s"],
        )

    started = time.perf_counter()
    results: list[dict[str, float] | None] = [None] * len(shards)
    with cf.ProcessPoolExecutor(max_workers=len(shards)) as executor:
        futures = {
            executor.submit(
                _copy_one,
                index,
                source,
                destination_root,
                copier,
                do_fsync,
                threads,
                cp_binary,
            ): index
            for index, source in enumerate(shards)
        }
        for future in cf.as_completed(futures):
            results[futures[future]] = future.result()
    wall = time.perf_counter() - started
    complete = [result for result in results if result is not None]
    return (
        wall,
        max((result["copy_s"] for result in complete), default=0.0),
        max((result["fsync_s"] for result in complete), default=0.0),
    )


# ------------------------------ benchmark ----------------------------------


def estimated_need(spec: dict[str, Any]) -> int:
    if spec["kind"] == "large":
        data_bytes = spec["total_bytes"]
        files = spec.get("shards", spec["concurrency"])
    else:
        data_bytes = spec["count"] * spec["file_size"]
        files = spec["count"]
    return data_bytes * 2 + 256 * MIB + files * 4096


def remove_scratch(path: str) -> None:
    if os.path.lexists(path):
        shutil.rmtree(path)


def run_case(
    ctx: RunContext,
    spec: dict[str, Any],
    args: argparse.Namespace,
    copiers: Sequence[Copier],
    cp_binary: str,
) -> None:
    workload = spec["workload"]
    concurrency = spec["concurrency"]
    mount = str(ctx.target.path)
    scratch = ctx.work_dir / args.scratch_subpath
    source_root = os.path.join(scratch, f"src_{workload}_C{concurrency}")
    destination_root = os.path.join(scratch, f"dst_{workload}_C{concurrency}")
    need = estimated_need(spec)
    free = shutil.disk_usage(mount).free
    if free < need:
        raise BenchmarkError(
            f"[{mount}] insufficient space for {workload} C={concurrency}: "
            f"free {free / MIB:.0f} MiB < need {need / MIB:.0f} MiB"
        )
    fs_type = fstype(mount)
    remove_scratch(source_root)
    remove_scratch(destination_root)
    try:
        os.makedirs(source_root, exist_ok=True)
        log(f"[{mount}] generate {workload} C={concurrency} ...")
        shards = gen_dataset(spec, source_root, args.data_pattern)
        total_bytes, total_files = dataset_bytes_files(shards)
        inventories = {shard: tree_inventory(shard) for shard in shards}
        log(
            f"[{mount}] {workload} C={concurrency}: "
            f"{total_bytes / MIB:.1f} MiB in {total_files} files"
        )
        warm_source(shards, total_bytes)
        log(
            f"[{mount}] {workload} C={concurrency}: "
            "source pre-read complete (warm cache, best effort)"
        )

        for copier in copiers:
            for repeat in range(1, args.repeats + 1):
                remove_scratch(destination_root)
                os.makedirs(destination_root, exist_ok=True)
                wall, copy_seconds, fsync_seconds = copy_shards(
                    shards,
                    destination_root,
                    copier,
                    args.fsync,
                    args.threads,
                    cp_binary,
                )
                for index, shard in enumerate(shards):
                    verify_tree(
                        shard,
                        os.path.join(destination_root, f"shard_{index}"),
                        inventories[shard],
                    )
                mib_s = total_bytes / MIB / wall if wall > 0 else 0.0
                files_s = total_files / wall if wall > 0 else 0.0
                ctx.emit(
                    f"{workload}/C{concurrency}/{copier.name}",
                    {
                        "total_bytes": total_bytes,
                        "total_files": total_files,
                        "wall_s": round(wall, 6),
                        "copy_s": round(copy_seconds, 6),
                        "fsync_s": round(fsync_seconds, 6),
                        "mib_s": round(mib_s, 3),
                        "files_s": round(files_s, 3),
                    },
                    repeat=repeat,
                    details={
                        "fstype": fs_type,
                        "workload": workload,
                        "kind": spec["kind"],
                        "concurrency": concurrency,
                        "threads": args.threads,
                        "data_pattern": args.data_pattern,
                        "copier": copier.name,
                        "copier_kind": copier.kind,
                        "copier_argv": list(copier.argv),
                        "fsync": args.fsync,
                        "cache_state": CACHE_STATE,
                        "verified": True,
                        "verification": "relative file paths and sizes",
                    },
                )
                log(
                    f"[{mount}] {workload} C={concurrency} {copier.name} "
                    f"rep{repeat}: wall={wall:.3f}s copy={copy_seconds:.3f}s "
                    f"fsync={fsync_seconds:.3f}s -> {mib_s:.1f} MiB/s, "
                    f"{files_s:.0f} files/s (verified)"
                )
    finally:
        if args.keep_scratch:
            log(
                f"[{mount}] keeping scratch: {source_root}, {destination_root}"
            )
        else:
            remove_scratch(source_root)
            remove_scratch(destination_root)


def build_plan(
    stage: str,
    smoke: bool,
    small_count: int,
    concurrency_levels: Sequence[int],
) -> list[dict[str, Any]]:
    if smoke:
        return [
            {
                "workload": "smoke_L8M",
                "kind": "large",
                "concurrency": 1,
                "total_bytes": 8 * MIB,
            },
            {
                "workload": "smoke_S200_x8K",
                "kind": "small",
                "concurrency": 1,
                "count": 200,
                "file_size": 8 * 1024,
            },
            {
                "workload": "smoke_L16M",
                "kind": "large",
                "concurrency": 4,
                "total_bytes": 16 * MIB,
            },
            {
                "workload": "smoke_S400_x8K",
                "kind": "small",
                "concurrency": 4,
                "count": 400,
                "file_size": 8 * 1024,
            },
        ]

    plan: list[dict[str, Any]] = []
    small_prefix = f"S{small_count}"
    if stage in ("single", "all"):
        plan.extend(
            [
                {
                    "workload": "L256M",
                    "kind": "large",
                    "concurrency": 1,
                    "total_bytes": 256 * MIB,
                },
                {
                    "workload": "L1G",
                    "kind": "large",
                    "concurrency": 1,
                    "total_bytes": 1 * GIB,
                },
                {
                    "workload": f"{small_prefix}_x4K",
                    "kind": "small",
                    "concurrency": 1,
                    "count": small_count,
                    "file_size": 4 * 1024,
                },
                {
                    "workload": f"{small_prefix}_x64K",
                    "kind": "small",
                    "concurrency": 1,
                    "count": small_count,
                    "file_size": 64 * 1024,
                },
            ]
        )
    if stage in ("concurrency", "all"):
        for concurrency in concurrency_levels:
            plan.extend(
                [
                    {
                        "workload": "Ltotal2G",
                        "kind": "large",
                        "concurrency": concurrency,
                        "total_bytes": 2 * GIB,
                    },
                    {
                        "workload": f"{small_prefix}_x64K",
                        "kind": "small",
                        "concurrency": concurrency,
                        "count": small_count,
                        "file_size": 64 * 1024,
                    },
                ]
            )
    return plan


def filter_plan(
    plan: list[dict[str, Any]], kinds: Sequence[str]
) -> list[dict[str, Any]]:
    if not kinds:
        return plan
    keep = set(kinds)
    unknown = keep - {"large", "small"}
    if unknown:
        raise BenchmarkError(
            "unknown kinds value(s): " + ", ".join(sorted(unknown))
        )
    return [spec for spec in plan if spec["kind"] in keep]


def dedupe_plan(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated specs produced by overlapping stages such as ``all``."""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for spec in plan:
        key = (
            spec["workload"],
            spec["kind"],
            spec["concurrency"],
            spec.get("total_bytes"),
            spec.get("count"),
            spec.get("file_size"),
            spec.get("shards"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(spec)
    return unique




def run(ctx: RunContext, args: argparse.Namespace) -> None:
    mount = str(ctx.target.path)
    scratch_subpath = os.path.normpath(args.scratch_subpath)
    if (
        scratch_subpath in ("", ".")
        or os.path.isabs(scratch_subpath)
        or scratch_subpath.startswith("..")
    ):
        raise BenchmarkError(
            "scratch_subpath must be relative and stay inside the run directory"
        )
    args.scratch_subpath = scratch_subpath
    scratch = (ctx.work_dir / scratch_subpath).resolve()
    if not scratch.is_relative_to(ctx.work_dir.resolve()):
        raise BenchmarkError("scratch directory escapes the run directory")

    copiers = resolve_copiers(args)
    cp_binary = args.cp_binary or shutil.which("cp") or ""
    if any(copier.kind in ("cp", "cpr") for copier in copiers):
        if not cp_binary:
            raise BenchmarkError("could not find cp; set CONFIG['cp_binary']")
        if not os.path.isfile(cp_binary) or not os.access(cp_binary, os.X_OK):
            raise BenchmarkError(f"cp binary is not executable: {cp_binary}")

    plan = dedupe_plan(
        filter_plan(
            build_plan(
                args.stage,
                args.smoke,
                args.small_count,
                args.conc_levels,
            ),
            args.kinds,
        )
    )
    if not plan:
        raise BenchmarkError("no benchmark cases selected")

    executable_hashes = {}
    for copier in copiers:
        executable = cp_binary if copier.kind in ("cp", "cpr") else (
            shutil.which(copier.argv[0]) if copier.kind == "external" else None
        )
        if copier.kind == "external" and not executable:
            raise BenchmarkError(f"external copier executable not found: {copier.argv[0]}")
        if executable and copier.name not in executable_hashes:
            with open(executable, "rb") as handle:
                executable_hashes[copier.name] = hashlib.file_digest(handle, "sha256").hexdigest()

    ctx.parameters.update({
        "executable_sha256": executable_hashes,
        "cache_state": CACHE_STATE,
        "resolved_copiers": [
            {"name": copier.name, "kind": copier.kind, "argv": list(copier.argv)}
            for copier in copiers
        ],
    })
    ctx.parameters.pop("cp_binary", None)
    log(
        f"=== mount={mount} stage={args.stage} "
        f"copiers={','.join(copier.name for copier in copiers)} "
        f"cache={CACHE_STATE} fsync={args.fsync} repeats={args.repeats} "
        f"cases={len(plan)} ==="
    )
    for spec in plan:
        run_case(ctx, spec, args, copiers, cp_binary)


# ------------------------------- command line -------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Same-filesystem copy/clone benchmark. Only --smoke is exposed; "
            "all other settings live in CONFIG at the top of the module."
        )
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="run four small real cases instead of the selected stage",
    )
    parsed = parser.parse_args(argv)
    namespace = argparse.Namespace(**CONFIG, **vars(parsed))
    if namespace.stage not in ("single", "concurrency", "all"):
        raise BenchmarkError("stage must be 'single', 'concurrency' or 'all'")
    if namespace.repeats < 1:
        raise BenchmarkError("repeats must be greater than zero")
    if namespace.threads < 1:
        raise BenchmarkError("threads must be greater than zero")
    if namespace.small_count < 1:
        raise BenchmarkError("small_count must be greater than zero")
    if namespace.data_pattern not in ("random", "zeros"):
        raise BenchmarkError("data_pattern must be 'random' or 'zeros'")
    if not namespace.conc_levels or any(
        level < 1 for level in namespace.conc_levels
    ):
        raise BenchmarkError("conc_levels must contain positive integers")
    return namespace
