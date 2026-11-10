"""Replay the pinned Linux workflow in per-task runner-owned checkouts.

Every task clones Linux into its own directory on its own machine, then all
tasks run the same configuration/build/command/commit workflow between
barriers. Configuration, builds, command loading/replay, commits and tree
assertions are measured. Cloning, checkout validation and reset/clean cleanup
are separate. No checkpoints or backend snapshots run; shell pipelines omit
pipefail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .build_settings import read_compiler_version

# Relative to each machine's checkout: no controller path may reach a task.
DEFAULT_FIXTURE_DIR = Path("data") / "dev-workflow"
DEFAULT_SERIES = "overlayfs-ovl-file.json"
DEFAULT_CONFIG = "tinyconfig-overlayfs.config"
DEFAULT_LINUX_URL = "https://github.com/torvalds/linux.git"

# One task's `make -j` uses exactly the CPUs it is pinned to. An unpinned value
# makes runs incomparable across machines; change both CONFIG keys together
# for a deliberate experiment.
DEFAULT_JOBS = 8

WORKFLOW_METRICS = ("elapsed_ms", "preparation_ms", "cleanup_ms", "unattributed_ms")

# ---------------------------------------------------------------------------
# Experiment configuration. These constants replace the former workload CLI
# flags; edit them in source for a deliberate experiment.
# ---------------------------------------------------------------------------
CONFIG = {
    "fixture_dir": str(DEFAULT_FIXTURE_DIR),
    "require_fstype": None,
    "tasks": 1,
    # Must equal "jobs": one pinned CPU per make job.
    "cpus_per_task": DEFAULT_JOBS,
    "jobs": DEFAULT_JOBS,
    "oversubscribe": 1,
    "verbose": False,
    "linux_url": DEFAULT_LINUX_URL,
}

FIGURES = [
    Figure("steps", "kind", "ms", kind="box", xlabel="Step kind",
           ylabel="Duration (s)", yscale=1000, split=("tasks",), name="step-duration-tasks"),
    Figure("workflow", "tasks", "elapsed_ms", kind="box", xlabel="Tasks",
           ylabel="Workflow (s)", yscale=1000, name="workflow-duration"),
]

def resolve_fixture_dir(value: str) -> Path:
    """Resolve the fixture directory in this machine's checkout.

    A relative name (the default) is resolved against this machine's project
    root; an absolute path must exist on every machine.
    """
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path

def resolve_linux_url(value: str) -> str:
    """Resolve a local Linux mirror on this machine; URLs pass through.

    Relative mirror paths are resolved against this machine's checkout, so a
    controller path never reaches a task on another machine. Anything with a
    colon before its first slash is a URL: ``https://`` and scp-like
    ``host:linux.git`` or ``user@host:linux.git``, as Git itself decides.
    """
    if re.match(r"^[^/]+:", value):
        return value
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else PROJECT_ROOT / path)

def check_build_parallelism(jobs: int, cpus_per_task: int) -> int:
    """Require one make job per pinned CPU; return the build parallelism."""
    if type(jobs) is not int or jobs < 1:
        raise ValueError(f"jobs must be a positive integer: {jobs!r}")
    if cpus_per_task != jobs:
        raise ValueError(
            f"cpus_per_task ({cpus_per_task}) must equal jobs ({jobs}): "
            "one make job per pinned CPU"
        )
    return jobs

CORE_COMMANDS = ("git", "bash", "make", "patch", "grep", "sed", "python3")
KERNEL_COMMANDS = ("bc", "bison", "flex", "perl", "openssl")
KERNEL_HEADERS = (
    ("OpenSSL development headers", ("/usr/include/openssl/ssl.h",)),
    ("libelf development headers", ("/usr/include/libelf.h", "/usr/include/gelf.h")),
)



class WorkflowError(RuntimeError):
    """A command, fixture check, or cleanup operation failed."""


@dataclass
class CommandResult:
    ms: int
    returncode: int
    stdout: str
    stderr: str




def elapsed_ms(start_ns: int) -> int:
    return (time.perf_counter_ns() - start_ns) // 1_000_000


def tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]




def terminate_process_group(
    proc: subprocess.Popen[Any],
    *,
    isolated_group: bool,
) -> None:
    if proc.poll() is not None:
        return
    if isolated_group:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
    else:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.communicate(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if isolated_group:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
    else:
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    what: str,
    verbose: bool = False,
    capture_stdout: bool = False,
    allow_codes: tuple[int, ...] = (0,),
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_CONFIG_KEY_1": "core.fsync",
            "GIT_CONFIG_VALUE_1": "committed",
            "GIT_CONFIG_KEY_2": "core.fsyncMethod",
            "GIT_CONFIG_VALUE_2": "fsync",
        }
    )
    if extra_env:
        env.update(extra_env)

    stdout = subprocess.PIPE if capture_stdout else (
        None if verbose else subprocess.DEVNULL
    )
    stderr = None if verbose else subprocess.PIPE
    stdin = subprocess.DEVNULL

    start = time.perf_counter_ns()
    isolated_group = True
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=isolated_group,
    )

    try:
        captured_stdout, captured_stderr = proc.communicate()
    except BaseException:
        terminate_process_group(proc, isolated_group=isolated_group)
        raise

    result = CommandResult(
        ms=elapsed_ms(start),
        returncode=proc.returncode,
        stdout=captured_stdout or "",
        stderr=captured_stderr or "",
    )
    if proc.returncode not in allow_codes:
        details = []
        if result.stdout:
            details.append(f"stdout:\n{tail(result.stdout)}")
        if result.stderr:
            details.append(f"stderr:\n{tail(result.stderr)}")
        suffix = "\n" + "\n".join(details) if details else ""
        raise WorkflowError(
            f"{what} failed with exit code {proc.returncode}: "
            f"{shlex.join(argv)}{suffix}"
        )
    return result


def run_shell(command: str, *, cwd: Path, what: str, verbose: bool) -> CommandResult:
    return run_command(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=cwd,
        what=what,
        verbose=verbose,
        extra_env={"LC_ALL": "C"},
    )


def run_git(repo: Path, args: list[str], *, what: str, verbose: bool) -> CommandResult:
    return run_command(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        what=what,
        verbose=verbose,
    )


def git_text(
    repo: Path,
    args: list[str],
    *,
    what: str,
    allow_codes: tuple[int, ...] = (0,),
) -> str:
    result = run_command(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        what=what,
        capture_stdout=True,
        allow_codes=allow_codes,
    )
    return result.stdout.strip()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError(f"fixture {path} must contain a JSON object")
    return value


def load_commands(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()

    if "%%" in lines:
        commands: list[str] = []
        current: list[str] = []
        for line in lines:
            if line == "%%":
                if current:
                    commands.append("\n".join(current) + "\n")
                    current = []
                continue
            if not current and line.lstrip().startswith("#"):
                continue
            current.append(line)
        if current:
            commands.append("\n".join(current) + "\n")
        return commands

    commands = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            commands.append(stripped + "\n")
    return commands

def fixture_hashes(fixture_dir: Path, names: set[str]) -> dict[str, str]:
    hashes = {}
    for name in sorted(names):
        with (fixture_dir / name).open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return hashes

def tool_versions(work_dir: Path) -> dict[str, str]:
    return {
        name: run_command(
            [name, "--version"], cwd=work_dir,
            what=f"reading {name} version", capture_stdout=True,
        ).stdout.strip().splitlines()[0]
        for name in ("git", "make")
    }


def validate_fixtures(fixture_dir: Path) -> dict[str, Any]:
    """Fail fast on missing fixtures before the expensive Linux preparation."""
    if not fixture_dir.is_dir():
        raise WorkflowError(
            f"fixture directory not found: {fixture_dir}\n"
            "Deploy data/dev-workflow with the package."
        )
    series_path = fixture_dir / DEFAULT_SERIES
    config_path = fixture_dir / DEFAULT_CONFIG
    series = load_json(series_path)
    if not config_path.is_file():
        raise WorkflowError(f"config fixture not found: {config_path}")

    base_commit = series.get("base_commit")
    commits = series.get("commits")
    if not isinstance(base_commit, str) or not base_commit:
        raise WorkflowError(f"{series_path}: invalid base_commit")
    if not isinstance(commits, list) or not commits:
        raise WorkflowError(f"{series_path}: invalid commits list")

    missing: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict) or not isinstance(commit.get("id"), str):
            raise WorkflowError(f"{series_path}: invalid commit entry")
        for command_file in ("search_commands", "read_commands", "edit_commands"):
            filename = commit.get(command_file)
            if not isinstance(filename, str) or not filename:
                raise WorkflowError(
                    f"{series_path}: commit {commit['id']} has no {command_file}"
                )
            if not (fixture_dir / filename).is_file():
                missing.append(filename)
    if missing:
        raise WorkflowError(
            "fixtures reference missing command files in "
            f"{fixture_dir}: {', '.join(sorted(set(missing)))}"
        )
    return series


def fixture_names(series: dict[str, Any]) -> set[str]:
    """Fixture files whose hashes identify the pinned series."""
    names = {DEFAULT_SERIES, DEFAULT_CONFIG}
    for commit in series["commits"]:
        names.update(commit[key] for key in
                     ("search_commands", "read_commands", "edit_commands"))
    return names


def missing_dependencies(*, require_fstype: bool = False) -> list[str]:
    missing = [name for name in CORE_COMMANDS if shutil.which(name) is None]
    if require_fstype and shutil.which("findmnt") is None:
        missing.append("findmnt")
    for name in KERNEL_COMMANDS:
        if shutil.which(name) is None:
            missing.append(name)
    for label, paths in KERNEL_HEADERS:
        if not any(Path(path).exists() for path in paths):
            missing.append(label)
    return missing



def ensure_dependencies(*, require_fstype: bool = False) -> None:
    missing = missing_dependencies(require_fstype=require_fstype)
    if missing:
        raise WorkflowError("missing dependencies (install manually): " + ", ".join(missing))




def resolve_commit(repo: Path, revision: str) -> str | None:
    """Resolve a revision expression (e.g. ``<sha>^``) to a full commit id.

    ``rev-parse --verify --quiet`` exits 1 for anything it cannot resolve, so
    missing objects stay a "not present" answer instead of an error.
    """
    result = run_command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "--verify",
            "--quiet",
            f"{revision}^{{commit}}",
        ],
        cwd=repo,
        what=f"resolving commit {revision}",
        capture_stdout=True,
        allow_codes=(0, 1),
    )
    return result.stdout.strip() or None


def has_local_commit(repo: Path, revision: str) -> bool:
    return resolve_commit(repo, revision) is not None


def prepare_linux_checkout(
    repo: Path,
    *,
    linux_url: str,
    base_commit: str,
    commit_ids: list[str],
    verbose: bool,
) -> None:
    """Clone the source into this task's directory and check out the base.

    ``linux_url`` is already resolved on this machine: a Git URL or a local
    mirror path. ``--no-local`` keeps the clone from sharing source objects, so
    every object is written to the measured filesystem.
    """
    if repo.exists():
        raise WorkflowError(f"runner checkout must be fresh: {repo}")
    run_command(
        ["git", "clone", "--no-local", linux_url, str(repo)],
        cwd=repo.parent,
        what="cloning Linux checkout",
        verbose=verbose,
    )
    required = [base_commit, *commit_ids]
    missing = [revision for revision in required if not has_local_commit(repo, revision)]
    if missing:
        raise WorkflowError("Linux source is missing fixture commits: " + ", ".join(missing))

    run_git(
        repo,
        ["clean", "-fdx"],
        what="cleaning checkout before preparation checkout",
        verbose=verbose,
    )
    run_git(
        repo,
        ["checkout", "--detach", "--force", base_commit],
        what="checking out fixture base commit",
        verbose=verbose,
    )
    run_git(
        repo,
        ["clean", "-fdx"],
        what="removing untracked preparation artifacts",
        verbose=verbose,
    )


def validate_repository(
    repo: Path,
    *,
    base_commit: str,
    commit_ids: list[str],
) -> None:
    if not repo.exists() or not repo.is_dir():
        raise WorkflowError(f"checkout is not a directory: {repo}")

    top_level = Path(
        git_text(
            repo,
            ["rev-parse", "--show-toplevel"],
            what="checking repository root",
        )
    ).resolve()
    if top_level != repo.resolve():
        raise WorkflowError(
            f"checkout must be the repository root, but its Git root is {top_level}"
        )

    shallow = git_text(
        repo,
        ["rev-parse", "--is-shallow-repository"],
        what="checking whether the fixture is shallow",
    )
    if shallow != "false":
        raise WorkflowError(
            f"checkout is shallow ({shallow}); a full Linux clone is required"
        )

    promisor = git_text(
        repo,
        ["config", "--bool", "--get", "remote.origin.promisor"],
        what="checking promisor configuration",
        allow_codes=(0, 1),
    )
    if promisor == "true":
        raise WorkflowError(
            "checkout is a partial clone; lazy object fetches could use the network"
        )

    partial = git_text(
        repo,
        ["config", "--get", "extensions.partialClone"],
        what="checking partial clone configuration",
        allow_codes=(0, 1),
    )
    if partial:
        raise WorkflowError(
            "checkout has extensions.partialClone; lazy fetches are not allowed"
        )

    sparse = git_text(
        repo,
        ["config", "--bool", "--get", "core.sparseCheckout"],
        what="checking sparse-checkout configuration",
        allow_codes=(0, 1),
    )
    if sparse == "true":
        raise WorkflowError("checkout uses sparse-checkout; a complete Linux tree is required")

    required = [base_commit, *commit_ids]
    for revision in required:
        run_git(
            repo,
            ["rev-parse", "--verify", f"{revision}^{{commit}}"],
            what=f"verifying local object for {revision}",
            verbose=False,
        )


def detect_filesystem(path: Path) -> str | None:
    """Report the filesystem backing ``path``.

    ``path`` may not exist yet (the checkout is absent before cloning),
    so the nearest existing ancestor is probed. ``None`` means it could not be
    determined, which is reported rather than silently guessed.
    """
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return None
    probe = existing_ancestor(path)
    proc = subprocess.run(
        [findmnt, "-T", str(probe), "-n", "-o", "FSTYPE"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    value = proc.stdout.strip()
    return value or None


def existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def require_filesystem(path: Path, expected: str | None, label: str) -> None:
    if not expected:
        return
    probe = existing_ancestor(path)
    actual = detect_filesystem(probe)
    if actual is None:
        raise WorkflowError(
            f"cannot determine filesystem for {label}; install findmnt or "
            "remove the filesystem requirement"
        )
    if actual != expected:
        raise WorkflowError(
            f"{label} must be on {expected}, but {probe} is on {actual}"
        )




def cleanup_checkout(
    repo: Path,
    *,
    base_commit: str,
    verbose: bool,
) -> None:
    errors: list[str] = []
    try:
        run_git(
            repo,
            ["reset", "--hard", base_commit],
            what="restoring checkout to fixture base commit",
            verbose=verbose,
        )
    except WorkflowError as exc:
        errors.append(str(exc))
    try:
        run_git(
            repo,
            ["clean", "-fdx"],
            what="removing generated and untracked files",
            verbose=verbose,
        )
    except WorkflowError as exc:
        errors.append(str(exc))


    if errors:
        raise WorkflowError("cleanup failed: " + "; ".join(errors))


def begin_checkout_run(
    repo: Path,
    *,
    base_commit: str,
) -> None:
    head = git_text(repo, ["rev-parse", "HEAD"], what="reading checkout HEAD")
    expected = resolve_commit(repo, base_commit)
    if expected is None:
        raise WorkflowError(
            f"fixture base commit {base_commit} is not present in {repo}"
        )
    if head != expected:
        raise WorkflowError(
            f"checkout HEAD is {head}, expected fixture base commit "
            f"{base_commit} ({expected})"
        )

    status = git_text(
        repo,
        [
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored",
        ],
        what="checking checkout cleanliness",
    )
    if status:
        preview = "\n".join(status.splitlines()[:20])
        raise WorkflowError(
            "checkout must be clean before a new run, including ignored and "
            f"untracked files:\n{preview}"
        )



def verify_tree(repo: Path, commit_id: str) -> dict[str, Any]:
    actual = git_text(
        repo,
        ["rev-parse", "HEAD^{tree}"],
        what=f"reading replayed tree for {commit_id}",
    )
    expected = git_text(
        repo,
        ["rev-parse", f"{commit_id}^{{tree}}"],
        what=f"reading target tree for {commit_id}",
    )
    if actual != expected:
        raise WorkflowError(
            f"tree mismatch for {commit_id}: expected {expected}, got {actual}"
        )
    return {"commit": commit_id, "tree": actual, "ok": True}




def prepare_checkout(
    repo: Path,
    *,
    linux_url: str,
    args: argparse.Namespace,
    series: dict[str, Any],
) -> tuple[str, int]:
    """Create and validate this task's checkout; never timed."""
    start = time.perf_counter_ns()
    base_commit_expression = series["base_commit"]
    commit_ids = [commit["id"] for commit in series["commits"]]
    require_filesystem(repo, args.require_fstype, "Linux checkout")
    prepare_linux_checkout(
        repo,
        linux_url=linux_url,
        base_commit=base_commit_expression,
        commit_ids=commit_ids,
        verbose=args.verbose,
    )
    validate_repository(
        repo, base_commit=base_commit_expression, commit_ids=commit_ids
    )
    # The fixture may name its base with a revision expression (the shipped
    # dev-workflow fixture uses ``c2c54b5f34f6^``). Everything after this point
    # compares full object ids, so resolve once here.
    base_commit = resolve_commit(repo, base_commit_expression)
    if base_commit is None:
        raise WorkflowError(
            f"fixture base commit {base_commit_expression} cannot be resolved "
            f"in {repo}"
        )
    begin_checkout_run(repo, base_commit=base_commit)
    return base_commit, elapsed_ms(start)


def run_workflow(
    repo: Path,
    *,
    args: argparse.Namespace,
    fixture_dir: Path,
    series: dict[str, Any],
    base_commit: str,
    preparation_ms: int,
) -> dict[str, Any]:
    """Run the timed workflow in `repo`; raise on any failure."""
    series_path = fixture_dir / DEFAULT_SERIES
    config_path = fixture_dir / DEFAULT_CONFIG
    commits = series["commits"]
    # Pinned by CONFIG so timings stay comparable across machines.
    jobs = args.jobs

    steps: list[dict[str, Any]] = []
    tree_assertions: list[dict[str, Any]] = []
    timed_start = time.perf_counter_ns()
    try:
        config_start = time.perf_counter_ns()
        run_command(
            ["make", "tinyconfig", f"-j{jobs}"],
            cwd=repo,
            what="make tinyconfig",
            verbose=args.verbose,
        )
        shutil.copyfile(config_path, repo / ".config")
        run_command(
            ["make", "olddefconfig"],
            cwd=repo,
            what="make olddefconfig",
            verbose=args.verbose,
        )
        steps.append({
            "kind": "config", "name": "config", "commit": "",
            "ms": elapsed_ms(config_start),
        })


        initial = run_command(
            ["make", f"-j{jobs}"],
            cwd=repo,
            what="initial build",
            verbose=args.verbose,
        )
        steps.append({
            "kind": "build", "name": "initial-build", "commit": "", "ms": initial.ms,
        })


        for commit in commits:
            commit_id = commit["id"]
            message = commit.get("message")
            files = commit.get("files")
            if not isinstance(message, str):
                raise WorkflowError(f"{series_path}: commit {commit_id} has no message")
            if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
                raise WorkflowError(f"{series_path}: commit {commit_id} has invalid files")

            commands: list[tuple[str, str]] = []
            for command_file in ("search_commands", "read_commands", "edit_commands"):
                filename = commit.get(command_file)
                if not isinstance(filename, str) or not filename:
                    raise WorkflowError(
                        f"{series_path}: commit {commit_id} has no {command_file}"
                    )
                commands.append((command_file, filename))

            for phase, filename in commands:
                category = phase.removesuffix("_commands")
                load_start = time.perf_counter_ns()
                loaded = load_commands(fixture_dir / filename)
                steps.append({
                    "kind": "command", "name": f"{category}-load",
                    "commit": commit_id, "ms": elapsed_ms(load_start),
                })
                for command in loaded:
                    result = run_shell(
                        command,
                        cwd=repo,
                        what=f"{category} command for {commit_id}",
                        verbose=args.verbose,
                    )
                    steps.append({
                        "kind": "command", "name": category,
                        "commit": commit_id, "ms": result.ms,
                    })

            incremental = run_command(
                ["make", f"-j{jobs}"],
                cwd=repo,
                what=f"incremental build for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "build", "name": "incremental-build",
                "commit": commit_id, "ms": incremental.ms,
            })


            status = run_git(
                repo,
                ["status", "--short"],
                what=f"git status for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "status", "commit": commit_id, "ms": status.ms,
            })

            diff = run_git(
                repo,
                ["diff", "--stat", "--", *files],
                what=f"git diff for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "diff", "commit": commit_id, "ms": diff.ms,
            })

            add = run_git(
                repo,
                ["add", "--", *files],
                what=f"git add for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "add", "commit": commit_id, "ms": add.ms,
            })

            commit_result = run_git(
                repo,
                [
                    "-c",
                    "user.name=Filesystem Bench",
                    "-c",
                    "user.email=bench@example.invalid",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    message,
                ],
                what=f"git commit for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "commit", "commit": commit_id,
                "ms": commit_result.ms,
            })

            verify_start = time.perf_counter_ns()
            tree_assertions.append(verify_tree(repo, commit_id))
            steps.append({
                "kind": "verify", "name": "tree-verify", "commit": commit_id,
                "ms": elapsed_ms(verify_start),
            })

        workflow_ms = elapsed_ms(timed_start)
        compiler_version = read_compiler_version(repo)
    finally:
        cleanup_start = time.perf_counter_ns()
        cleanup_checkout(repo, base_commit=base_commit, verbose=args.verbose)
        cleanup_ms = elapsed_ms(cleanup_start)

    unattributed_ms = max(0, workflow_ms - sum(step["ms"] for step in steps))

    return {
        "base_commit": base_commit,
        "compiler_version": compiler_version,
        "elapsed_ms": workflow_ms,
        "preparation_ms": preparation_ms,
        "cleanup_ms": cleanup_ms,
        "unattributed_ms": unattributed_ms,
        "tree_assertions": tree_assertions,
        "steps": steps,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the kernel workflow in one disposable Linux checkout per "
            "task on the filesystem under test."
        )
    )
    parser.parse_args(argv)
    args = argparse.Namespace(**CONFIG)
    check_build_parallelism(args.jobs, args.cpus_per_task)
    return args


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    cluster.run_tasks(ctx, args)


def task(t: cluster.Task) -> None:
    """Clone, prepare and run one kernel workflow instance on this machine."""
    args = t.args
    fixture_dir = resolve_fixture_dir(args.fixture_dir)
    series = validate_fixtures(fixture_dir)
    if t.leader:
        ensure_dependencies(require_fstype=bool(args.require_fstype))
        versions = tool_versions(PROJECT_ROOT)
    else:
        versions = None
    t.node_barrier()

    repo = t.task_dir / "repo"
    base_commit, preparation_ms = prepare_checkout(
        repo,
        linux_url=resolve_linux_url(args.linux_url),
        args=args,
        series=series,
    )
    t.log(f"task {t.index} on {t.node}: checkout ready in {preparation_ms} ms")

    t.barrier()
    summary = run_workflow(
        repo,
        args=args,
        fixture_dir=fixture_dir,
        series=series,
        base_commit=base_commit,
        preparation_ms=preparation_ms,
    )

    results = t.gather((t.node, summary))
    if t.leader:
        t.node_info({
            "fixture_sha256": fixture_hashes(fixture_dir, fixture_names(series)),
            "tool_versions": {**(versions or {}), "compiler": summary["compiler_version"]},
        })
    else:
        t.node_info(None)
    if t.index != 0:
        return
    first = results[0][1]
    t.info({
        "jobs": args.jobs,
        "cpus_per_task": args.cpus_per_task,
        "base_commit": first["base_commit"],
        "tree_assertions": first["tree_assertions"],
    })
    for sample, (host, result) in enumerate(results):
        t.emit("workflow", {"tasks": t.count, "sample": sample, "host": host},
               {key: result[key] for key in WORKFLOW_METRICS})
        for step, entry in enumerate(result["steps"], 1):
            t.emit("steps", {
                "tasks": t.count, "sample": sample, "host": host,
                "step": step, "name": entry["name"],
                "kind": entry["kind"], "commit": entry["commit"],
            }, {"ms": entry["ms"]})
