"""Offline npm/pnpm materialization from prewarmed external local sources.

Each batch starts independent projects together. Every worker has a private
cache/store and measures command execution and, when ``CONFIG['flush']`` is
set, a full project fsync. Source preparation, project creation, inventories
and cleanup are untimed.
The internal pool width and external project concurrency are separate inputs;
neither is a claim about the package manager's total thread count.

This is not an empty-cache sandbox install or a physical write-bandwidth test.
Registry traffic and lifecycle scripts are excluded; target FS traffic remains.
"""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing
import os
import resource
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from agent_fs_bench.benchmarks.clone import fstype

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
    "pm": "both",
    "runs": 10,
    "concurrency": 4,
    "concurrent_jobs": [1],
    "prep_dir": "/dev/shm",
    "npm_bin": "npm",
    "pnpm_bin": "pnpm",
    "tool_versions": {"node": "v24.22.2", "npm": "11.19.1", "pnpm": "12.16.1"},
    "ignore_scripts": True,
    "keep_node_modules": False,
    "seed_only": False,
    "flush": True,
}

FIGURES = [
    Figure("batches", "concurrent_jobs", "install_mean_s", split=("manager",),
           xlabel="Concurrent installs", ylabel="Install time (s)", logx=True),
    Figure("batches", "concurrent_jobs", "durable_mean_s", split=("manager",),
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
    log(f"prewarming cache/store: {path}")
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
    for name in list(env):
        if name.lower().startswith("npm_config_") or name in {
            "NODE_ENV", "NODE_OPTIONS", "UV_THREADPOOL_SIZE", "RAYON_NUM_THREADS",
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


def run_visible(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    log("run: " + " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        check=False,
    )
    if result.returncode != 0:
        die(f"command failed with exit code {result.returncode}")


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
) -> None:
    """Seed the npm cache from a fixture's manifest and npm lockfile.

    The vendored fixture ships pnpm's lockfile plus an npm lockfile resolved
    from the same manifest (`--legacy-peer-deps`, which these trees need: e.g.
    `eslint@9` against `@typescript-eslint/parser@7`). The committed npm
    lockfile is required; missing fixtures are never re-resolved.
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
    run_visible(command, seed_dir, env=env)

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


def install_worker(
    command: list[str], project: Path, log_path: Path, env: dict[str, str],
    barrier: Any, connection: Any, flush: bool = True,
) -> None:
    """One Python process owns one child and its resource-usage counters."""
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            connection.send({"ready": True})
            barrier.wait()
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            started = time.monotonic()
            result = subprocess.run(
                command, cwd=project, env=env, stdout=log_handle,
                stderr=subprocess.STDOUT, check=False,
            )
            exited = time.monotonic()
            if result.returncode != 0:
                die(f"install exited {result.returncode}; {log_path}\n{read_tail(log_path)}")
            if flush:
                fsync_tree(project)
                durable = time.monotonic()
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
        sample = {
            "started": started,
            "install_s": exited - started,
            "user_s": after.ru_utime - before.ru_utime,
            "sys_s": after.ru_stime - before.ru_stime,
        }
        if flush:
            sample["flush_s"] = durable - exited
            sample["durable_s"] = durable - started
        connection.send(sample)
    except Exception as exc:
        connection.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


def run_workers(jobs: list[dict[str, Any]]) -> list[dict[str, int | float]]:
    """Release ready workers together; retain actual start offsets."""
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(len(jobs) + 1)
    processes = []
    connections = []
    try:
        for job in jobs:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=install_worker,
                args=(job["command"], job["project"], job["log"], job["env"], barrier, sender, job["flush"]),
            )
            process.start()
            sender.close()
            processes.append(process)
            connections.append(receiver)
        for connection in connections:
            ready = connection.recv()
            if "error" in ready:
                die(ready["error"])
        batch_started = time.monotonic()
        barrier.wait()
        tasks = []
        for task_number, connection in enumerate(connections, 1):
            result = connection.recv()
            if "error" in result:
                die(result["error"])
            result["start_offset_s"] = result.pop("started") - batch_started
            result["task"] = task_number
            tasks.append(result)
        for process in processes:
            process.join()
            if process.exitcode != 0:
                die(f"install worker exited {process.exitcode}")
        return tasks
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join()
        for connection in connections:
            connection.close()


def batch_metrics(
    tasks: list[dict[str, int | float]], trees: list[dict[str, int]],
    flush: bool = True,
) -> dict[str, int | float]:
    count = len(tasks)
    install_s = max(task["start_offset_s"] + task["install_s"] for task in tasks)
    phases = ("install", "flush", "durable") if flush else ("install",)
    metrics = {
        f"{phase}_mean_s": sum(task[f"{phase}_s"] for task in tasks) / count
        for phase in phases
    }
    metrics.update({
        "batch_install_s": install_s,
        "installs_per_s": count / install_s,
        "user_s": sum(task["user_s"] for task in tasks),
        "sys_s": sum(task["sys_s"] for task in tasks),
    })
    if flush:
        durable_s = max(task["start_offset_s"] + task["durable_s"] for task in tasks)
        metrics.update({
            "batch_durable_s": durable_s,
            "durable_installs_per_s": count / durable_s,
        })
    return {
        **{key: round(value, 6) for key, value in metrics.items()},
        **{key: sum(tree[key] for tree in trees) for key in trees[0]},
    }


def run_batch(
    ctx: RunContext, args: argparse.Namespace, manager: str, binary: str,
    source: Path, template: Path, prep: Path, count: int, repeat: int,
) -> tuple[dict[str, int | float], list[dict[str, int | float]]]:
    name = f"{manager}-c{count}-r{repeat}"
    source_batch = prep / "batches" / name
    project_batch = ctx.work_dir / name
    jobs = []
    for task_number in range(1, count + 1):
        sandbox = source_batch / f"task-{task_number}"
        task_source = sandbox / "source"
        shutil.copytree(source, task_source)
        project = prepare_project(project_batch / f"task-{task_number}", manager, template)
        env = manager_env(manager, args.concurrency, sandbox)
        if manager == "pnpm":
            env["XDG_CACHE_HOME"] = str(task_source / "cache")
        check_project_placement(project, ctx.target.path)
        if args.flush:
            sync_prepared_project(project, ctx.target.path)
        jobs.append({
            "project": project, "env": env,
            "command": install_command(manager, binary, task_source, args.ignore_scripts),
            "log": ctx.result_dir / "logs" / f"{name}-task-{task_number}.log",
            "flush": args.flush,
        })
    # No source writes, metrics or deletions overlap timed workers.
    if args.flush:
        sync_filesystem()
    for task_number in range(1, count + 1):
        warm_tree(source_batch / f"task-{task_number}" / "source")
    log(f"{manager}: {count} projects, batch {repeat}/{args.runs}")
    tasks = run_workers(jobs)
    trees = [tree_metrics(job["project"] / "node_modules") for job in jobs]
    metrics = batch_metrics(tasks, trees, flush=args.flush)
    if not args.keep_node_modules:
        remove_tree(project_batch)
    remove_tree(source_batch)
    if args.flush:
        sync_filesystem()
    return metrics, tasks

def task_metrics(task: dict[str, int | float], flush: bool) -> dict[str, int | float]:
    names = ("install_s", "flush_s", "durable_s") if flush else ("install_s",)
    return {**{name: task[name] for name in names},
            **{name: task[name] for name in ("user_s", "sys_s", "start_offset_s")}}

def parse_args(argv: list[str], config: dict[str, Any] = CONFIG) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline installation batches; edit CONFIG for experiment settings."
    )
    namespace = argparse.Namespace(**config, **vars(parser.parse_args(argv)))
    # Duplicate sizes would write rows with identical keys and double-weight the summary.
    if len(set(namespace.concurrent_jobs)) != len(namespace.concurrent_jobs) or any(
            count < 1 for count in namespace.concurrent_jobs):
        die("concurrent_jobs must be distinct positive batch sizes")
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


def checked_tools(args: argparse.Namespace, managers: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    binaries = {manager: resolve_tool(getattr(args, f"{manager}_bin"), manager) for manager in managers}
    versions = {manager: command_version(binary) for manager, binary in binaries.items()}
    versions["node"] = command_version(resolve_tool("node", "node"))
    for tool, version in versions.items():
        if version != args.tool_versions[tool]:
            die(f"{tool} version {version} does not match pinned {args.tool_versions[tool]}")
    return binaries, versions


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    if not Path("/proc/self/mountinfo").is_file():
        die("npm-install requires Linux; run it inside the benchmark client or Colima")
    target = ctx.target.path.resolve()
    prep_root = Path(args.prep_dir).expanduser().resolve()
    if not prep_root.is_dir():
        die(f"preparation directory does not exist: {prep_root}")
    target_type, prep_type = check_placement(target, prep_root)
    managers = [manager for manager in ("npm", "pnpm") if args.pm in (manager, "both")]
    binaries, versions = checked_tools(args, managers)
    prep = prep_root / "agent-fs-bench" / ctx.benchmark / ctx.run_id / ctx.target.name
    prep.mkdir(parents=True, exist_ok=False)
    ensure_dir(ctx.result_dir / "logs")
    fixture = fixture_dir_for(args.fixture)
    sources, templates, fingerprints = {}, {}, {}
    for manager in managers:
        source = ensure_dir(prep / "sources" / manager)
        template = ensure_dir(prep / "templates" / manager)
        seed = ensure_dir(prep / "seeds" / manager)
        if manager == "npm":
            seed_npm_fixture(
                binaries[manager], ensure_dir(source / "cache"), seed, template,
                fixture, args.ignore_scripts, args.concurrency,
            )
        else:
            seed_pnpm_fixture(
                binaries[manager], ensure_dir(source / "store"), ensure_dir(source / "cache"),
                seed, template, fixture, args.ignore_scripts, args.concurrency,
            )
        lockfile = "package-lock.json" if manager == "npm" else "pnpm-lock.yaml"
        fingerprints[manager] = {
            name: hashlib.sha256((template / name).read_bytes()).hexdigest()
            for name in ("package.json", lockfile)
            if (template / name).is_file()
        }
        sources[manager], templates[manager] = source, template
    ctx.info.update({
        "target_fstype": target_type,
        "prep_fstype": prep_type,
        "cpu_count": len(os.sched_getaffinity(0)),
        "tool_versions": {
            manager: {tool: versions[tool] for tool in ("node", manager)}
            for manager in managers
        },
        "seed_input_sha256": fingerprints,
    })
    if args.flush:
        sync_filesystem()
    if args.seed_only:
        log(f"seed complete; sources retained at {prep}")
        return
    for manager in managers:
        for count in args.concurrent_jobs:
            for repeat in range(1, args.runs + 1):
                metrics, tasks = run_batch(
                    ctx, args, manager, binaries[manager], sources[manager],
                    templates[manager], prep, count, repeat,
                )
                keys = {"manager": manager, "concurrent_jobs": count, "repeat": repeat}
                ctx.emit("batches", keys, metrics)
                for task in tasks:
                    ctx.emit("tasks", {**keys, "sample": task["task"]},
                             task_metrics(task, args.flush))
    remove_tree(prep)
