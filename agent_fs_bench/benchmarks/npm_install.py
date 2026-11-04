"""Benchmark warm npm/pnpm caches against cold node_modules trees.

The setup phase populates an npm cache and pnpm store. Timed runs then use
an empty node_modules directory on the target filesystem:

    npm install --offline
    pnpm install --offline --package-import-method=copy \
      --config.enable-global-virtual-store=false

Each completed install emits one JSONL measurement through the shared runner.
Cache/store setup and prewarming happen outside the timed section.

Two placements are supported, both selected through ``CONFIG['prep_dir']``:

* same filesystem (default, ``prep_dir`` None) -- cache/store live next to
  the timed node_modules, so the filesystem may hardlink or server-side clone
  package contents instead of writing them.
* cross filesystem (``prep_dir`` set) -- npm cache, pnpm store, pnpm XDG
  cache, templates and seed projects move to another filesystem, so every
  timed write lands on the target filesystem as real data. The placement is
  verified with ``st_dev`` and the run is aborted if it was not achieved.

``pnpm`` always imports packages with ``copy``; ``hardlink``/``auto`` can
short-circuit the write path this benchmark is meant to measure.

``CONFIG['concurrency']`` (default 4) pins the parallelism knobs that actually
govern a timed offline install. The managers do not share one knob, so N
is applied per manager:

* npm has **no** install-wide limit and no option for one: Arborist fans
  every pending package out to pacote at once. Its writes are async
  ``fs.write`` calls, so ``UV_THREADPOOL_SIZE`` is the one pin available;
  at 4 the default is unchanged, and lowering it measurably serialises
  the writes (1 is ~20% slower on the reference tree).
* pnpm's store-to-``node_modules`` copy is what the timed install mostly
  does. ``RAYON_NUM_THREADS`` sizes that pool (Linux/Windows);
  ``PNPM_MACOS_LINK_THREADS`` sizes the separate macOS copy pool, whose
  default of 4 is why the two are set together -- on macOS the rayon
  variable alone changes nothing.

``PNPM_MAX_WORKERS`` (pnpm 11's Node worker pool, unread by pnpm 12) and
``--network-concurrency`` / ``--child-concurrency`` (which bound downloads
and lifecycle scripts, not an offline materialisation) are deliberately not
set. A concurrency of 0 keeps each tool's own defaults instead.

After each timed install the tree is fsynced file by file and directory by
directory. That flush is timed separately as ``sync_s`` and is not part of
``elapsed_s``.

Only committed fixtures are installed: ``CONFIG['fixture']`` names one of the
vendored trees under ``data/``, and its committed lockfiles define the
payload. The public CLI exposes no workload flags; edit ``CONFIG`` for a
deliberate experiment setup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_fs_bench.results import PROJECT_ROOT, RunContext

PNPM_IMPORT_METHOD = "copy"
DEFAULT_CONCURRENCY = 4
# Upstream pnpm fixtures vendored under data/, so the benchmark can replay the
# dependency trees pnpm's own install benchmark uses.
FIXTURES = {
    "pnpm-bench": "data/pnpm-bench",
    "pnpm-bench-integrated": "data/pnpm-bench-integrated",
}
CONCURRENCY_NOTE = (
    "one N is pinned onto the knobs with a measured effect: npm gets "
    "UV_THREADPOOL_SIZE (its unpack fan-out itself has no limit or option), "
    "pnpm gets RAYON_NUM_THREADS and PNPM_MACOS_LINK_THREADS. The download "
    "and lifecycle-script limits and the pnpm 11 worker pool are not set "
    "because they do not affect a timed offline install"
)

# ---------------------------------------------------------------------------
# CONFIG: all experiment settings for this workload.
#
# The public CLI exposes no workload flags; change these values (in code) to
# run a different experiment setup. ``fixture`` names a committed tree from
# ``FIXTURES``. ``prep_dir`` is None for same-filesystem placement; point it
# at an existing directory on another filesystem for cross-filesystem
# placement (``allow_same_device`` permits a same-device prep_dir).
# ---------------------------------------------------------------------------
CONFIG: dict[str, Any] = {
    "fixture": "pnpm-bench",
    "pm": "both",
    "runs": 5,
    "concurrency": DEFAULT_CONCURRENCY,
    "prep_dir": None,
    "allow_same_device": False,
    "npm_bin": "npm",
    "pnpm_bin": "pnpm",
    "ignore_scripts": True,
    "no_prewarm": False,
    "warm_once": False,
    "keep_node_modules": False,
    "seed_only": False,
}



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
    try:
        result = subprocess.run(
            [command, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchError(f"cannot obtain version of {command}: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        die(f"{command} --version failed: {result.stderr.strip()}")
    return result.stdout.strip()


def sync_filesystem() -> None:
    os.sync()


def raise_walk_error(error: OSError) -> None:
    raise error


def fsync_tree(path: Path) -> tuple[float, int]:
    """Fsync every file and directory under ``path``; return (seconds, errors).

    Opening a directory read-only and fsyncing the descriptor persists its
    entries. Walking bottom-up means a directory is synced after the entries
    it gained. The walk itself is not timed: only the fsync calls are, so
    ``elapsed_s`` and ``sync_s`` stay additive.
    """
    if not path.is_dir():
        die(f"installed node_modules tree is missing: {path}")

    directories: list[Path] = []
    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False, onerror=raise_walk_error):
        directories.append(Path(dirpath))
        for filename in filenames:
            files.append(Path(dirpath) / filename)

    elapsed = 0.0

    def sync_path(item: Path) -> None:
        nonlocal elapsed
        descriptor = os.open(item, os.O_RDONLY)
        try:
            started = time.monotonic()
            os.fsync(descriptor)
            elapsed += time.monotonic() - started
        finally:
            os.close(descriptor)

    for file_path in files:
        sync_path(file_path)
    for directory in reversed(directories):
        sync_path(directory)
    return elapsed, 0


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


def npm_fixture_args(args: argparse.Namespace) -> list[str]:
    """Extra npm flags a fixture tree needs.

    pnpm's own fixtures pin dependency versions whose peer ranges conflict
    under npm's resolver (e.g. `eslint@9` against `@typescript-eslint/parser@7`
    in the pnpm-bench fixture), so a fixture run has to resolve the same way
    the seed did.
    """
    return ["--legacy-peer-deps"] if args.fixture else []


def concurrency_for(
    manager: str, concurrency: int
) -> tuple[dict[str, str], list[str], str]:
    """Return (env, extra args, description) pinning ``manager`` to ``concurrency``.

    ``concurrency`` of 0 keeps the tool's own defaults and returns empty
    overrides. The managers do not share a single knob, so one value is
    mapped onto the ones that measurably matter; see the module docstring
    and docs/bench-npm-install.md for the measurements behind this table.

    Only knobs with a measured effect are set. Dropped on purpose:

    * ``PNPM_MAX_WORKERS`` -- the pnpm 11 Node worker pool; pnpm 12's binary
      does not read it at all.
    * ``--network-concurrency`` / ``--child-concurrency`` -- these bound
      download and lifecycle-script concurrency, which the offline timed
      install barely uses; measured effect under 1%.
    """
    if concurrency < 1:
        return {}, [], "tool default"

    if manager == "npm":
        # npm has no install-wide limit: Arborist fans every pending package
        # out to pacote at once. The libuv pool is what its async file system
        # calls actually run on, so it is the only pin available.
        env = {"UV_THREADPOOL_SIZE": str(concurrency)}
        return env, [], f"UV_THREADPOOL_SIZE={concurrency}"

    env = {
        # Linux/Windows: rayon copies packages out of the store.
        "RAYON_NUM_THREADS": str(concurrency),
        # macOS: a separate, smaller pool does the copying (default 4).
        "PNPM_MACOS_LINK_THREADS": str(concurrency),
    }
    return env, [], f"RAYON_NUM_THREADS={concurrency} PNPM_MACOS_LINK_THREADS={concurrency}"


def manager_env(manager: str, concurrency: int) -> dict[str, str] | None:
    """Process environment for one install, or ``None`` to inherit unchanged."""
    env, _args, _description = concurrency_for(manager, concurrency)
    if not env:
        return None
    merged = os.environ.copy()
    merged.update(env)
    return merged


def concurrency_args(manager: str, concurrency: int) -> list[str]:
    return concurrency_for(manager, concurrency)[1]




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
    """Return the vendored fixture directory named by ``CONFIG['fixture']``."""
    if name not in FIXTURES:
        die(
            f"unknown fixture {name!r}; choose from: "
            + ", ".join(sorted(FIXTURES))
        )
    path = PROJECT_ROOT / FIXTURES[name]
    if not path.is_dir():
        die(f"fixture {name} is missing: {path}")
    return path


def copy_fixture_manifest(
    fixture_dir: Path, destination_dir: Path, lockfile: str
) -> None:
    ensure_dir(destination_dir)
    for name in ("package.json", lockfile, "pnpm-workspace.yaml"):
        source = fixture_dir / name
        destination = destination_dir / name
        if source.is_file():
            shutil.copy2(source, destination)
        elif name == "pnpm-workspace.yaml":
            destination.unlink(missing_ok=True)
        else:
            die(f"required fixture input is missing: {source}")


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
        "install",
        "--legacy-peer-deps",
        "--no-audit",
        "--no-fund",
        "--cache",
        str(cache_dir),
        *concurrency_args("npm", concurrency),
        *ignore_args(ignore_scripts),
    ]
    run_visible(command, seed_dir, env=manager_env("npm", concurrency))

    package_lock = seed_dir / "package-lock.json"
    if not package_lock.is_file():
        die("npm fixture seed did not create package-lock.json")
    shutil.copy2(seed_dir / "package.json", templates_dir / "package.json")
    shutil.copy2(package_lock, templates_dir / "package-lock.json")
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
        *concurrency_args("pnpm", concurrency),
        *ignore_args(ignore_scripts),
    ]
    env = manager_env("pnpm", concurrency)
    if env is None:
        env = os.environ.copy()
    env["XDG_CACHE_HOME"] = str(cache_dir)
    run_visible(command, seed_dir, env=env)

    template_dir.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
        source = seed_dir / name
        if source.is_file():
            shutil.copy2(source, template_dir / name)
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


def prepare_project(
    root: Path,
    manager: str,
    run_number: int,
    template_dir: Path,
) -> Path:
    # Same-filesystem mode keeps timed and seed projects as siblings.
    # Cross-filesystem mode moves the seed projects to CONFIG['prep_dir'].
    project = root / "work" / f"{manager}-run-{run_number}"
    ensure_dir(project)
    shutil.copy2(template_dir / "package.json", project / "package.json")
    if manager == "npm":
        shutil.copy2(template_dir / "package-lock.json", project / "package-lock.json")
    else:
        shutil.copy2(template_dir / "pnpm-lock.yaml", project / "pnpm-lock.yaml")
        # A fixture workspace manifest carries settings the fixture needs
        # (`supportedArchitectures`, `enableGlobalVirtualStore`). Its
        # `storeDir` is overridden by the explicit --store-dir.
        workspace_manifest = template_dir / "pnpm-workspace.yaml"
        if workspace_manifest.is_file():
            shutil.copy2(workspace_manifest, project / "pnpm-workspace.yaml")
    remove_tree(project / "node_modules")
    return project


def read_tail(path: Path, line_count: int = 20) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-line_count:])


def run_timed(
    command: list[str],
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> tuple[int, float, float, float]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            result = subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        status = result.returncode
    except OSError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        status = 1
    elapsed = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    user = max(0.0, after.ru_utime - before.ru_utime)
    system = max(0.0, after.ru_stime - before.ru_stime)
    return status, elapsed, user, system


def finalize_run(
    project: Path, keep_node_modules: bool
) -> tuple[float, int, dict[str, int]]:
    """Flush the tree, measure it while it exists, then drop it."""
    node_modules = project / "node_modules"
    sync_s, sync_errors = fsync_tree(node_modules)
    metrics = tree_metrics(node_modules)
    if not keep_node_modules:
        remove_tree(node_modules)
    sync_filesystem()
    return sync_s, sync_errors, metrics


def make_result(
    elapsed: float,
    sync_s: float,
    sync_errors: int,
    user: float,
    system: float,
    metrics: dict[str, int],
) -> dict[str, int | float]:
    elapsed_for_rate = elapsed if elapsed > 0 else 0.0
    files_per_s = metrics["files"] / elapsed_for_rate if elapsed_for_rate else 0.0
    logical_mib_s = (
        (metrics["logical_bytes"] / 1048576.0) / elapsed_for_rate
        if elapsed_for_rate
        else 0.0
    )
    allocated_mib_s = (
        (metrics["allocated_bytes"] / 1048576.0) / elapsed_for_rate
        if elapsed_for_rate
        else 0.0
    )
    sync_for_rate = sync_s if sync_s > 0 else 0.0
    sync_files_per_s = (
        metrics["files"] / sync_for_rate if sync_for_rate else 0.0
    )
    return {
        "elapsed_s": round(elapsed, 6),
        "sync_s": round(sync_s, 6),
        "sync_errors": sync_errors,
        "user_s": round(user, 6),
        "sys_s": round(system, 6),
        **metrics,
        "files_per_s": round(files_per_s, 3),
        "sync_files_per_s": round(sync_files_per_s, 3),
        "logical_mib_per_s": round(logical_mib_s, 3),
        "allocated_mib_per_s": round(allocated_mib_s, 3),
    }


def run_npm_once(
    args: argparse.Namespace,
    root: Path,
    results_dir: Path,
    npm_bin: str,
    cache_dir: Path,
    template_dir: Path,
    run_number: int,
) -> tuple[dict[str, int | float], dict[str, object]]:
    project = prepare_project(root, "npm", run_number, template_dir)
    log_path = results_dir / "logs" / f"npm-run-{run_number}.log"
    if args.warm_each_run:
        warm_tree(cache_dir)

    command = [
        npm_bin,
        "install",
        "--offline",
        "--cache",
        str(cache_dir),
        "--no-audit",
        "--no-fund",
        "--no-update-notifier",
        *(npm_fixture_args(args)),
        *concurrency_args("npm", args.concurrency),
        *ignore_args(args.ignore_scripts),
    ]
    log(f"npm run {run_number}/{args.runs} on {project}")
    status, elapsed, user, system = run_timed(
        command, project, log_path, env=manager_env("npm", args.concurrency)
    )
    if status != 0:
        die(f"npm run {run_number} failed (exit {status}); {log_path}\n{read_tail(log_path)}")
    sync_s, sync_errors, metrics = finalize_run(project, args.keep_node_modules)
    row = make_result(
        elapsed,
        sync_s,
        sync_errors,
        user,
        system,
        metrics,
    )
    return row, {
        "command": command,
        "concurrency_environment": concurrency_for("npm", args.concurrency)[0],
    }


def run_pnpm_once(
    args: argparse.Namespace,
    root: Path,
    results_dir: Path,
    pnpm_bin: str,
    store_dir: Path,
    pnpm_cache: Path,
    template_dir: Path,
    run_number: int,
) -> tuple[dict[str, int | float], dict[str, object]]:
    project = prepare_project(root, "pnpm", run_number, template_dir)
    log_path = results_dir / "logs" / f"pnpm-run-{run_number}.log"
    if args.warm_each_run:
        warm_tree(store_dir)
        warm_tree(pnpm_cache)

    env = manager_env("pnpm", args.concurrency)
    if env is None:
        env = os.environ.copy()
    env["XDG_CACHE_HOME"] = str(pnpm_cache)
    command = [
        pnpm_bin,
        "install",
        "--offline",
        "--store-dir",
        str(store_dir),
        "--cache-dir",
        str(pnpm_cache),
        f"--package-import-method={PNPM_IMPORT_METHOD}",
        "--config.enable-global-virtual-store=false",
        "--frozen-lockfile",
        "--reporter=append-only",
        *concurrency_args("pnpm", args.concurrency),
        *ignore_args(args.ignore_scripts),
    ]
    log(f"pnpm run {run_number}/{args.runs} on {project}")
    status, elapsed, user, system = run_timed(command, project, log_path, env=env)
    if status != 0:
        die(f"pnpm run {run_number} failed (exit {status}); {log_path}\n{read_tail(log_path)}")
    sync_s, sync_errors, metrics = finalize_run(project, args.keep_node_modules)
    row = make_result(
        elapsed,
        sync_s,
        sync_errors,
        user,
        system,
        metrics,
    )
    return row, {
        "command": command,
        "concurrency_environment": {
            **concurrency_for("pnpm", args.concurrency)[0],
            "XDG_CACHE_HOME": str(pnpm_cache),
        },
    }




def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark warm-cache offline npm/pnpm installs on a target "
            "filesystem. No workload flags are exposed; edit CONFIG at the "
            "top of the module."
        )
    )
    parsed = parser.parse_args(argv)
    namespace = argparse.Namespace(**CONFIG, **vars(parsed))
    namespace.warm_each_run = (
        not namespace.no_prewarm and not namespace.warm_once
    )
    if namespace.fixture not in FIXTURES:
        raise BenchError(
            f"unknown fixture {namespace.fixture!r}; choose from: "
            + ", ".join(sorted(FIXTURES))
        )
    if namespace.pm not in ("npm", "pnpm", "both"):
        raise BenchError("pm must be 'npm', 'pnpm' or 'both'")
    if namespace.runs < 1:
        raise BenchError("runs must be greater than zero")
    if namespace.concurrency < 0:
        raise BenchError("concurrency must be zero or greater")
    return namespace


def device_of(path: Path) -> int:
    return path.stat().st_dev




def check_placement(
    target_dir: Path,
    prep_dir: Path | None,
    allow_same_device: bool,
) -> str:
    """Validate the requested cache placement against actual device IDs."""
    target_device = device_of(target_dir)
    if prep_dir is None:
        log("placement: same filesystem (prep_dir is None)")
        return "same"

    prep_device = device_of(prep_dir)
    if prep_device == target_device:
        if not allow_same_device:
            die(
                f"prep_dir {prep_dir} is on the same filesystem as target "
                f"{target_dir} (both device {target_device}); the filesystem "
                "can then hardlink or clone package contents instead of "
                "writing them. Point CONFIG['prep_dir'] at another filesystem, "
                "or set CONFIG['allow_same_device'] to run anyway."
            )
        log(
            "warning: prep_dir shares the target filesystem; cache reads keep "
            "competing with the timed writes"
        )
        return "cross-same-device"

    log(f"placement: cross filesystem ({target_device} -> {prep_device})")
    return "cross"


def same_device_of(first: Path, second: Path) -> bool:
    try:
        return device_of(first) == device_of(second)
    except OSError:
        return False


def make_layout(
    root: Path, prep_dir: Path | None
) -> dict[str, object]:
    """Build the cache/store/template/work layout for one placement mode.

    In cross-filesystem mode everything the timed install only reads lives on
    ``prep_dir``; only each run's ``node_modules`` is written to ``root``.
    """
    if prep_dir is None:
        cache_root = root / "cache"
        return {
            "npm_cache": ensure_dir(cache_root / "npm"),
            "pnpm_store": ensure_dir(cache_root / "pnpm"),
            "pnpm_cache": ensure_dir(cache_root / "pnpm-xdg"),
            "npm_template": ensure_dir(root / "templates" / "npm"),
            "pnpm_template": ensure_dir(root / "templates" / "pnpm"),
            "work_root": ensure_dir(root / "work"),
        }

    npm_cache = ensure_dir(prep_dir / "cache" / "npm")
    # The pnpm store and its XDG cache must share a filesystem, so they are
    # always built as siblings under prep_dir.
    pnpm_store = ensure_dir(prep_dir / "cache" / "pnpm")
    pnpm_cache = ensure_dir(prep_dir / "cache" / "pnpm-xdg")
    if not same_device_of(pnpm_store, pnpm_cache):
        die(
            f"pnpm store {pnpm_store} and pnpm XDG cache {pnpm_cache} are on "
            "different filesystems; they must share one"
        )
    return {
        "npm_cache": npm_cache,
        "pnpm_store": pnpm_store,
        "pnpm_cache": pnpm_cache,
        "npm_template": ensure_dir(prep_dir / "templates" / "npm"),
        "pnpm_template": ensure_dir(prep_dir / "templates" / "pnpm"),
        "work_root": ensure_dir(prep_dir / "work"),
    }


def warn_if_pnpm_split(npm_cache: Path, pnpm_store: Path) -> None:
    if same_device_of(npm_cache, pnpm_store):
        return
    log(
        "note: the npm cache and the pnpm store are on different filesystems; "
        "each manager is then measured with its own cache placement"
    )


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    target_dir = ctx.target.path
    prep_dir = None
    if args.prep_dir:
        prep_root = Path(args.prep_dir).expanduser().resolve()
        if not prep_root.is_dir():
            die(f"preparation directory does not exist: {prep_root}; mount it first")
        prep_dir = prep_root / "agent-fs-bench" / "npm-install" / ctx.run_id / ctx.target.name
        if not prep_dir.resolve().is_relative_to(prep_root):
            die(f"preparation directory escapes its root: {prep_dir}")
        prep_dir.mkdir(parents=True, exist_ok=False)
    placement = check_placement(target_dir, prep_dir, args.allow_same_device)
    root = ctx.work_dir
    results_dir = ctx.result_dir

    layout = make_layout(root, prep_dir)
    npm_cache = layout["npm_cache"]
    pnpm_store = layout["pnpm_store"]
    pnpm_cache = layout["pnpm_cache"]
    npm_template = layout["npm_template"]
    pnpm_template = layout["pnpm_template"]
    work_root = layout["work_root"]
    warn_if_pnpm_split(npm_cache, pnpm_store)
    seed_npm_dir = work_root / "seed-npm"
    seed_pnpm_dir = work_root / "seed-pnpm"
    # Reuse caches, not stale manifests/locks/workspace settings from another tree.
    for directory in (npm_template, pnpm_template, seed_npm_dir, seed_pnpm_dir):
        remove_tree(directory)
        ensure_dir(directory)
    ensure_dir(results_dir / "logs")

    fixture_dir = fixture_dir_for(args.fixture)
    required_locks = []
    if args.pm in ("npm", "both"):
        required_locks.append("package-lock.json")
    if args.pm in ("pnpm", "both"):
        required_locks.append("pnpm-lock.yaml")
    for name in required_locks:
        if not (fixture_dir / name).is_file():
            die(f"fixture {args.fixture} is missing {name}: {fixture_dir}")

    npm_bin = resolve_tool(args.npm_bin, "npm") if args.pm in ("npm", "both") else ""
    pnpm_bin = resolve_tool(args.pnpm_bin, "pnpm") if args.pm in ("pnpm", "both") else ""
    versions = {}
    if npm_bin:
        versions["npm"] = command_version(npm_bin)
    if pnpm_bin:
        versions["pnpm"] = command_version(pnpm_bin)
    versions["node"] = command_version(resolve_tool("node", "node"))
    ctx.parameters.update({
        "tool_versions": versions,
        "fixture": args.fixture,
        "placement": placement,
        "pnpm_import_method": PNPM_IMPORT_METHOD,
        "pnpm_global_virtual_store": False,
        "concurrency_note": CONCURRENCY_NOTE,
        "cache_state": "warm-cache-cold-node_modules" if not args.no_prewarm else "unwarmed-cache-cold-node_modules",
        "sync_mode": "fsync every file and directory under node_modules",
        "sync_timing": "sum of fsync calls only, excluded from elapsed_s",
    })
    # Local preparation roots and executable locations vary by machine; they
    # are not experiment identity. Tool versions and the fixture name remain.
    ctx.parameters.pop("prep_dir", None)
    ctx.parameters.pop("npm_bin", None)
    ctx.parameters.pop("pnpm_bin", None)

    if args.pm in ("npm", "both"):
        seed_npm_fixture(
            npm_bin,
            npm_cache,
            seed_npm_dir,
            npm_template,
            fixture_dir,
            args.ignore_scripts,
            args.concurrency,
        )
    if args.pm in ("pnpm", "both"):
        seed_pnpm_fixture(
            pnpm_bin,
            pnpm_store,
            pnpm_cache,
            seed_pnpm_dir,
            pnpm_template,
            fixture_dir,
            args.ignore_scripts,
            args.concurrency,
        )

    inputs = {}
    for manager, template in (("npm", npm_template), ("pnpm", pnpm_template)):
        if args.pm not in (manager, "both"):
            continue
        lock_name = "package-lock.json" if manager == "npm" else "pnpm-lock.yaml"
        manifest = json.loads((template / "package.json").read_text(encoding="utf-8"))
        hashes = {
            name: hashlib.sha256((template / name).read_bytes()).hexdigest()
            for name in ("package.json", lock_name, "pnpm-workspace.yaml")
            if (template / name).is_file()
        }
        inputs[manager] = {
            "package_json": manifest,
            "input_sha256": hashes,
            "workspace_yaml": (template / "pnpm-workspace.yaml").read_text(encoding="utf-8")
            if (template / "pnpm-workspace.yaml").is_file() else None,
        }
    ctx.parameters["resolved_inputs"] = inputs
    ctx.parameters["fixture_input_sha256"] = {
        name: hashlib.sha256((fixture_dir / name).read_bytes()).hexdigest()
        for name in ("package.json", "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml")
        if (fixture_dir / name).is_file()
    }
    placement_details = {
        "placement": placement,
        "target_device": device_of(target_dir),
        "prep_device": device_of(prep_dir) if prep_dir else None,
        "paths": {name: str(path) for name, path in layout.items()},
        "devices": {name: device_of(path) for name, path in layout.items()},
        "work_dir": str(root),
    }

    if not args.no_prewarm:
        if args.pm in ("npm", "both"):
            warm_tree(npm_cache)
        if args.pm in ("pnpm", "both"):
            warm_tree(pnpm_store)
            warm_tree(pnpm_cache)

    log("flushing seed/cache writes before timed runs")
    sync_filesystem()

    if args.seed_only:
        log("seed complete; timed runs skipped (seed_only)")
        log(f"root: {root}")
        return

    managers = []
    if args.pm in ("npm", "both"):
        managers.append("npm")
    if args.pm in ("pnpm", "both"):
        managers.append("pnpm")

    for manager in managers:
        for run_number in range(1, args.runs + 1):
            if manager == "npm":
                row, command_details = run_npm_once(
                    args,
                    root,
                    results_dir,
                    npm_bin,
                    npm_cache,
                    npm_template,
                    run_number,
                )
            else:
                row, command_details = run_pnpm_once(
                    args,
                    root,
                    results_dir,
                    pnpm_bin,
                    pnpm_store,
                    pnpm_cache,
                    pnpm_template,
                    run_number,
                )
            ctx.emit(
                manager,
                row,
                repeat=run_number,
                details={
                    "manager": manager,
                    "placement": placement_details,
                    **command_details,
                    "ignore_scripts": args.ignore_scripts,
                    "prewarm": not args.no_prewarm,
                    "warm_each_run": args.warm_each_run,
                    "returncode": 0,
                },
            )
