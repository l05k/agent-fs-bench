"""Offline npm/pnpm materialization from prewarmed external local sources.

Every task installs into its own project at the same time, on its assigned machine.
Each machine seeds its own source from the registry; each task copies
that source into a private sandbox for every repeat, so no task reads a cache,
store, home or project another task wrote. The command and, when
``CONFIG['flush']`` is set, a full project fsync are timed; source preparation,
project creation, prereading, inventories and cleanup are not.
The internal pool width (``concurrency``) and the external task count (``tasks``)
are separate inputs; neither is a claim about the package manager's total thread
count.

This is not an empty-cache sandbox install or a physical write-bandwidth test.
Registry traffic and lifecycle scripts are excluded; target FS traffic remains.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import resource
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from agent_fs_bench.benchmarks.clone import fstype

MANAGERS = ("npm", "pnpm")
PNPM_IMPORT_METHOD = "copy"
LOCAL_PREP_FILESYSTEMS = {"tmpfs", "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs"}
# Upstream pnpm fixture vendored under data/, so the benchmark can replay the
# dependency tree pnpm's own install benchmark uses.
FIXTURES = {
    "pnpm-bench": "data/pnpm-bench",
}

# All experiment settings are code-only. Change the version pins deliberately
# when changing runtimes; the benchmark never installs tools automatically.
CONFIG: dict[str, Any] = {
    "fixture": "pnpm-bench",
    "pm": "npm",
    "repeats": 10,
    # External concurrency: simultaneous independent installs, never swept in one run.
    "tasks": 1,
    "concurrency": 4,
    "prep_dir": "/dev/shm",
    "npm_bin": "npm",
    "pnpm_bin": "pnpm",
    "tool_versions": {"node": "v24.22.2", "npm": "11.19.1", "pnpm": "12.16.1"},
    "ignore_scripts": True,
    "keep_node_modules": False,
    "seed_only": False,
    "flush": True,
}

# Figures draw every install row (median and IQR), never a pre-computed batch mean.
FIGURES = [
    Figure("installs", "tasks", "install_s", split=("manager",),
           xlabel="Concurrent installs", ylabel="Install time (s)", logx=True),
    Figure("installs", "tasks", "durable_s", split=("manager",),
           xlabel="Concurrent installs", ylabel="Durable install time (s)", logx=True),
]


class BenchError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[bench] {message}", flush=True)


def die(message: str) -> None:
    raise BenchError(message)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_tool(value: str, label: str) -> str:
    found = shutil.which(value)
    if not found:
        die(f"{label} not found: {value}")
    return str(Path(found).resolve())


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    elif path.is_dir():
        shutil.rmtree(path)


def command_version(command: str, timeout: float = 10.0) -> str:
    result = subprocess.run(
        [command, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        die(f"{command} --version failed: {result.stderr.strip()}")
    return result.stdout.strip()


def sync_filesystem() -> None:
    os.sync()


def raise_walk_error(error: OSError) -> None:
    raise error


def fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(path: Path) -> float:
    """Sync regular files and bottom-up directories without following links.

    The entire walk/open/fsync/close sequence is timed. Symlink entries are
    persisted through their containing directories, not by syncing targets.
    """
    started = time.monotonic()
    if not path.is_dir():
        die(f"installed project tree is missing: {path}")
    for dirpath, _dirnames, filenames in os.walk(
        path, topdown=False, followlinks=False, onerror=raise_walk_error
    ):
        directory = Path(dirpath)
        for filename in filenames:
            file_path = directory / filename
            if stat.S_ISREG(file_path.lstat().st_mode):
                fsync_path(file_path)
        fsync_path(directory)
    return time.monotonic() - started


def check_project_placement(project: Path, target: Path) -> None:
    """Reject a project outside the measured target; independent of sync mode."""
    project, target = project.resolve(), target.resolve()
    if not project.is_relative_to(target):
        die(f"project is outside the measured target: {project}")


def sync_prepared_project(project: Path, target: Path) -> None:
    """Persist input files and every newly created ancestor before timing."""
    project, target = project.resolve(), target.resolve()
    check_project_placement(project, target)
    fsync_tree(project)
    ancestor = project.parent
    while True:
        fsync_path(ancestor)
        if ancestor == target:
            break
        ancestor = ancestor.parent


def warm_tree(path: Path) -> None:
    """Read cache/store files; page residency is still best effort."""
    if not path.is_dir():
        die(f"cache/store directory is missing: {path}")
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False, onerror=raise_walk_error):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            with file_path.open("rb") as handle:
                while handle.read(1024 * 1024):
                    pass


def ignore_args(ignore_scripts: bool) -> list[str]:
    return ["--ignore-scripts"] if ignore_scripts else []


def concurrency_env(manager: str, concurrency: int) -> dict[str, str]:
    """Pin the pool that materializes files, not package fan-out.

    npm uses its libuv pool. pnpm 12 is a Rust binary and sizes its global
    Rayon pool from ``RAYON_NUM_THREADS``; the link phase runs on that pool on
    Linux. pnpm's ``child-concurrency``, ``network-concurrency`` and
    ``workspace-concurrency`` govern lifecycle scripts, registry requests and
    workspace tasks, none of which the timed install exercises.
    """
    if concurrency == 0:
        return {}
    if manager == "npm":
        return {"UV_THREADPOOL_SIZE": str(concurrency)}
    return {"RAYON_NUM_THREADS": str(concurrency)}


def manager_env(manager: str, concurrency: int, sandbox: Path) -> dict[str, str]:
    """Use private local HOME/TMP and explicit package manager configuration."""
    env = os.environ.copy()
    # The pnpm worker-pool names are JS-era (pnpm 10/11); pnpm 12 ignores them,
    # but they stay scrubbed so an inherited value can never reach a manager.
    for name in list(env):
        if name.lower().startswith("npm_config_") or name in {
            "NODE_ENV", "NODE_OPTIONS", "UV_THREADPOOL_SIZE", "RAYON_NUM_THREADS",
            "PNPM_MAX_WORKERS", "PNPM_WORKERS",
        }:
            del env[name]
    env.update(concurrency_env(manager, concurrency))
    env.update({
        "HOME": str(ensure_dir(sandbox / "home")),
        "TMPDIR": str(ensure_dir(sandbox / "tmp")),
        "XDG_CACHE_HOME": str(ensure_dir(sandbox / "xdg")),
        "XDG_CONFIG_HOME": str(ensure_dir(sandbox / "config")),
        "XDG_DATA_HOME": str(ensure_dir(sandbox / "data")),
        "NPM_CONFIG_USERCONFIG": str(sandbox / "user.npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(sandbox / "global.npmrc"),
        "CI": "1",
        "NO_UPDATE_NOTIFIER": "1",
    })
    return env


def run_visible(command: list[str], cwd: Path, log_path: Path,
                env: dict[str, str] | None = None) -> None:
    """Run a command whose output belongs in a per-task log file, not the console."""
    log("run: " + " ".join(command))
    with log_path.open("w", encoding="utf-8") as handle:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        die(f"command failed with exit code {result.returncode}: {log_path}\n{read_tail(log_path)}")


def fixture_dir_for(name: str) -> Path:
    path = PROJECT_ROOT / FIXTURES[name]
    if not path.is_dir():
        die(f"fixture {name} is missing: {path}")
    return path


def copy_fixture_manifest(
    fixture_dir: Path, destination_dir: Path, lockfile: str
) -> None:
    ensure_dir(destination_dir)
    for name in ("package.json", lockfile):
        source = fixture_dir / name
        if not source.is_file():
            die(f"required fixture input is missing: {source}")
        shutil.copy2(source, destination_dir / name)


def seed_npm_fixture(
    npm_bin: str,
    cache_dir: Path,
    seed_dir: Path,
    templates_dir: Path,
    fixture_dir: Path,
    ignore_scripts: bool,
    concurrency: int,
    log_path: Path,
) -> None:
    """Seed the npm cache from the fixture's manifest and npm lockfile.

    The vendored fixture ships pnpm's lockfile plus an npm lockfile resolved
    from the same manifest (`--legacy-peer-deps`, which this tree needs: e.g.
    `eslint@9` against `@typescript-eslint/parser@7`). The committed npm
    lockfile is required; a missing one is never re-resolved.
    """
    ensure_dir(seed_dir)
    shutil.copy2(fixture_dir / "package.json", seed_dir / "package.json")
    fixture_lockfile = fixture_dir / "package-lock.json"
    if not fixture_lockfile.is_file():
        die(f"fixture npm lockfile is missing: {fixture_lockfile}")
    shutil.copy2(fixture_lockfile, seed_dir / "package-lock.json")

    command = [
        npm_bin,
        "ci",
        "--legacy-peer-deps",
        "--no-audit",
        "--no-fund",
        "--cache",
        str(cache_dir),
        *ignore_args(ignore_scripts),
    ]
    run_visible(
        command, seed_dir,
        log_path,
        env=manager_env("npm", concurrency, seed_dir.parent / "npm-env"),
    )

    if (seed_dir / "package-lock.json").read_bytes() != fixture_lockfile.read_bytes():
        die("npm seed changed the committed lockfile")
    copy_fixture_manifest(fixture_dir, templates_dir, "package-lock.json")
    remove_tree(seed_dir / "node_modules")


def seed_pnpm_fixture(
    pnpm_bin: str,
    store_dir: Path,
    cache_dir: Path,
    seed_dir: Path,
    template_dir: Path,
    fixture_dir: Path,
    ignore_scripts: bool,
    concurrency: int,
    log_path: Path,
) -> None:
    """Seed the pnpm store from the fixture's committed lockfile.

    The lockfile is taken as-is: ``--frozen-lockfile`` keeps the measured tree
    identical to what pnpm's own benchmark installs, and a rewrite here would
    silently change the payload.
    """
    ensure_dir(seed_dir)
    copy_fixture_manifest(fixture_dir, seed_dir, "pnpm-lock.yaml")
    command = [
        pnpm_bin,
        "install",
        "--frozen-lockfile",
        "--store-dir",
        str(store_dir),
        "--cache-dir",
        str(cache_dir),
        f"--package-import-method={PNPM_IMPORT_METHOD}",
        "--config.enable-global-virtual-store=false",
        "--reporter=append-only",
        *ignore_args(ignore_scripts),
    ]
    env = manager_env("pnpm", concurrency, seed_dir.parent / "pnpm-env")
    env["XDG_CACHE_HOME"] = str(cache_dir)
    run_visible(command, seed_dir, log_path, env=env)

    if (seed_dir / "pnpm-lock.yaml").read_bytes() != (fixture_dir / "pnpm-lock.yaml").read_bytes():
        die("pnpm seed changed the committed lockfile")
    copy_fixture_manifest(fixture_dir, template_dir, "pnpm-lock.yaml")
    remove_tree(seed_dir / "node_modules")


def tree_metrics(node_modules: Path) -> dict[str, int]:
    metrics = {
        "files": 0,
        "dirs": 0,
        "symlinks": 0,
        "hardlinks": 0,
        "logical_bytes": 0,
        "allocated_bytes": 0,
        "du_kib": 0,
    }
    if not node_modules.is_dir():
        die(f"installed node_modules tree is missing: {node_modules}")

    seen_allocated_inodes: set[tuple[int, int]] = set()
    metrics["dirs"] = 1
    stack = [node_modules]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries_iter:
            entries = list(entries_iter)
        for entry in entries:
            stat_result = entry.stat(follow_symlinks=False)
            if os.path.islink(entry.path):
                metrics["symlinks"] += 1
            elif os.path.isdir(entry.path):
                metrics["dirs"] += 1
                stack.append(Path(entry.path))
            elif os.path.isfile(entry.path):
                metrics["files"] += 1
                metrics["logical_bytes"] += stat_result.st_size
                if stat_result.st_nlink > 1:
                    inode = (stat_result.st_dev, stat_result.st_ino)
                    counted = inode not in seen_allocated_inodes
                    seen_allocated_inodes.add(inode)
                else:
                    counted = True
                if counted:
                    metrics["allocated_bytes"] += (
                        getattr(stat_result, "st_blocks", 0) or 0
                    ) * 512
                if stat_result.st_nlink > 1:
                    metrics["hardlinks"] += 1

    result = subprocess.run(
        ["du", "-sk", str(node_modules)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    metrics["du_kib"] = int(result.stdout.split()[0])

    return metrics


def prepare_project(project: Path, manager: str, template_dir: Path) -> Path:
    lockfile = "package-lock.json" if manager == "npm" else "pnpm-lock.yaml"
    copy_fixture_manifest(template_dir, project, lockfile)
    return project


def read_tail(path: Path, line_count: int = 20) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:])


def install_command(
    manager: str, binary: str, source: Path, ignore_scripts: bool,
) -> list[str]:
    if manager == "npm":
        return [
            binary, "ci", "--offline", "--cache", str(source / "cache"),
            "--legacy-peer-deps", "--no-audit", "--no-fund", "--no-update-notifier",
            *ignore_args(ignore_scripts),
        ]
    return [
        binary, "install", "--offline", "--frozen-lockfile",
        "--store-dir", str(source / "store"), "--cache-dir", str(source / "cache"),
        f"--package-import-method={PNPM_IMPORT_METHOD}",
        "--config.enable-global-virtual-store=false", "--reporter=append-only",
        *ignore_args(ignore_scripts),
    ]


def install(
    command: list[str], project: Path, log_path: Path, env: dict[str, str],
    flush: bool, host: str,
) -> dict[str, int | float | str]:
    """Run this task's install, then flush the project when asked.

    Returns one sample: command wall time, optional flush and durable times, the
    child CPU time this task's process accumulated, and the machine it ran on.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sample: dict[str, int | float | str] = {"host": host}
    with log_path.open("w", encoding="utf-8") as log_handle:
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=project, env=env, stdout=log_handle,
            stderr=subprocess.STDOUT, check=False,
        )
        exited = time.monotonic()
        if result.returncode != 0:
            die(f"install exited {result.returncode}; {log_path}\n{read_tail(log_path)}")
        sample["install_s"] = exited - started
        if flush:
            fsync_tree(project)
            durable = time.monotonic()
            sample["flush_s"] = durable - exited
            sample["durable_s"] = durable - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
    sample["user_s"] = after.ru_utime - before.ru_utime
    sample["sys_s"] = after.ru_stime - before.ru_stime
    return sample


def install_metrics(sample: dict[str, int | float | str], tree: dict[str, int],
                    flush: bool) -> dict[str, int | float]:
    """One install: its own timings, child CPU time and resulting node_modules inventory."""
    names = ("install_s", "flush_s", "durable_s") if flush else ("install_s",)
    return {**{name: sample[name] for name in (*names, "user_s", "sys_s")}, **tree}


def batch_metrics(tasks: int, wall: float) -> dict[str, float]:
    """One barrier-aligned batch: `wall_s` from the release barrier to the barrier after
    every task's install (and flush, with `flush`); `installs_per_s = tasks / wall_s`."""
    return {"wall_s": wall, "installs_per_s": tasks / wall}


def node_prep_dir(t: cluster.Task) -> Path:
    """This machine's preparation root for this run; never shared between machines."""
    prep_root = Path(t.args.prep_dir).expanduser().resolve()
    return prep_root / "agent-fs-bench" / t.benchmark / t.run_id / t.target.name / t.node


def prepare_node(t: cluster.Task) -> dict[str, Any]:
    """Seed this machine's source from the registry and report its facts.

    Runs on the node leader only: the target placement, the tool versions and
    the seed are properties of the machine that will do the work.
    """
    args = t.args
    if not Path("/proc/self/mountinfo").is_file():
        die(f"{t.node}: npm-install requires Linux")
    target = t.target.path.resolve()
    prep_root = Path(args.prep_dir).expanduser().resolve()
    if not prep_root.is_dir():
        die(f"{t.node}: preparation directory does not exist: {prep_root}")
    target_type, prep_type = check_placement(target, prep_root)
    binary, versions = checked_tools(args)
    prep = node_prep_dir(t)
    prep.mkdir(parents=True, exist_ok=False)
    source = ensure_dir(prep / "source")
    template = ensure_dir(prep / "template")
    seed = ensure_dir(prep / "seeds")
    fixture = fixture_dir_for(args.fixture)
    seed_log = t.log_dir / f"seed-{args.pm}.log"
    if args.pm == "npm":
        seed_npm_fixture(
            binary, ensure_dir(source / "cache"), seed, template,
            fixture, args.ignore_scripts, args.concurrency, seed_log,
        )
    else:
        seed_pnpm_fixture(
            binary, ensure_dir(source / "store"), ensure_dir(source / "cache"),
            seed, template, fixture, args.ignore_scripts, args.concurrency, seed_log,
        )
    lockfile = "package-lock.json" if args.pm == "npm" else "pnpm-lock.yaml"
    fingerprints = {
        name: hashlib.sha256((template / name).read_bytes()).hexdigest()
        for name in ("package.json", lockfile)
        if (template / name).is_file()
    }
    return {
        "target_fstype": target_type,
        "prep_fstype": prep_type,
        "tool_versions": versions,
        "seed_input_sha256": fingerprints,
    }


def run_repeat(t: cluster.Task, args: argparse.Namespace, repeat: int, binary: str) -> None:
    """One install round: private sandbox and project, barrier, install, inventory."""
    manager = args.pm
    prep = node_prep_dir(t)
    sandbox = prep / "repeats" / f"repeat-{repeat:04d}" / f"task-{t.index:04d}"
    source = sandbox / "source"
    shutil.copytree(prep / "source", source)
    project = prepare_project(t.task_dir / f"repeat-{repeat:04d}", manager, prep / "template")
    env = manager_env(manager, args.concurrency, sandbox)
    if manager == "pnpm":
        env["XDG_CACHE_HOME"] = str(source / "cache")
    check_project_placement(project, t.target.path)
    if args.flush:
        sync_prepared_project(project, t.target.path)
    t.node_barrier()
    if t.leader and args.flush:
        sync_filesystem()
    warm_tree(source)
    if t.index == 0:
        t.log(f"{manager}: repeat {repeat}/{args.repeats}, {t.count} install(s)")
    wall, samples = t.batch(
        install, install_command(manager, binary, source, args.ignore_scripts),
        project, t.log_dir / f"{manager}-repeat-{repeat:04d}-task-{t.index:04d}.log",
        env, args.flush, t.node,
    )
    trees = t.gather(tree_metrics(project / "node_modules"))
    if t.index == 0:
        keys = {"manager": manager, "tasks": t.count, "repeat": repeat}
        t.emit("batches", keys, batch_metrics(t.count, wall))
        for index, (sample, tree) in enumerate(zip(samples, trees), 1):
            t.emit("installs", {**keys, "sample": index, "host": sample["host"]},
                   install_metrics(sample, tree, args.flush))
    if not args.keep_node_modules:
        remove_tree(project)
    remove_tree(sandbox)


def task(t: cluster.Task) -> None:
    """Prepare this machine once, then run every repeat on this task."""
    args = t.args
    binary = resolve_tool(getattr(args, f"{args.pm}_bin"), args.pm)
    info = prepare_node(t) if t.leader else None
    t.node_barrier()
    t.node_info(info)
    if args.seed_only:
        if t.leader:
            t.log(f"{args.pm}: seed complete; source retained at {node_prep_dir(t)}")
        return
    for repeat in range(1, args.repeats + 1):
        run_repeat(t, args, repeat, binary)
    t.node_barrier()
    if t.leader:
        remove_tree(node_prep_dir(t).parent)


def parse_args(argv: list[str], config: dict[str, Any] = CONFIG) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline installs at one external task count; edit CONFIG for "
                    "experiment settings."
    )
    namespace = argparse.Namespace(**config, **vars(parser.parse_args(argv)))
    if namespace.pm not in MANAGERS:
        die(f"pm must be one of {', '.join(MANAGERS)}: {namespace.pm!r}")
    if type(namespace.repeats) is not int or namespace.repeats < 1:
        die(f"repeats must be a positive integer: {namespace.repeats!r}")
    if type(namespace.concurrency) is not int or namespace.concurrency < 0:
        die(f"concurrency must be a non-negative integer: {namespace.concurrency!r}")
    return namespace


def check_placement(target: Path, prep: Path) -> tuple[str, str]:
    """Different device IDs alone do not exclude cross-mount copy offload."""
    target_type, prep_type = fstype(target), fstype(prep)
    if prep_type not in LOCAL_PREP_FILESYSTEMS:
        die(f"prep_dir must use a local filesystem, not {prep_type}: {prep}")
    if target.stat().st_dev == prep.stat().st_dev or target_type == prep_type:
        die("prep_dir must use a different device and filesystem type from the target")
    log(f"placement: external local {prep_type} -> {target_type}")
    return target_type, prep_type


def checked_tools(args: argparse.Namespace) -> tuple[str, dict[str, str]]:
    """Resolve the manager binary and enforce the pinned versions on this machine."""
    manager = args.pm
    binary = resolve_tool(getattr(args, f"{manager}_bin"), manager)
    versions = {manager: command_version(binary)}
    versions["node"] = command_version(resolve_tool("node", "node"))
    for tool, version in versions.items():
        if version != args.tool_versions[tool]:
            die(f"{tool} version {version} does not match pinned {args.tool_versions[tool]}")
    return binary, versions


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    cluster.run_tasks(ctx, args)
