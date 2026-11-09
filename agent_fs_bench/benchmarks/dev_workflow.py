"""Replay the pinned Linux workflow in a runner-owned checkout.

Configuration, builds, command loading/replay, commits and tree assertions are
measured. Preparation and reset/clean cleanup are separate. No checkpoints or
backend snapshots run; shell pipelines omit pipefail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .build_settings import read_compiler_version

DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "data" / "dev-workflow"
DEFAULT_SERIES = "overlayfs-ovl-file.json"
DEFAULT_CONFIG = "tinyconfig-overlayfs.config"
DEFAULT_LINUX_URL = "https://github.com/torvalds/linux.git"

# Fixed build parallelism: an unpinned value makes runs incomparable across
# machines. Change CONFIG["jobs"] only for a deliberate experiment.
DEFAULT_JOBS = 8

# ---------------------------------------------------------------------------
# Experiment configuration. These constants replace the former workload CLI
# flags; edit them in source for a deliberate experiment.
# ---------------------------------------------------------------------------
CONFIG = {
    "fixture_dir": str(DEFAULT_FIXTURE_DIR),
    "require_fstype": None,
    "jobs": DEFAULT_JOBS,
    "verbose": False,
    "linux_url": DEFAULT_LINUX_URL,
}

FIGURES = [
    Figure("steps", "kind", "ms", kind="box", xlabel="Step kind",
           ylabel="Duration (s)", yscale=1000, name="step-duration"),
]

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


def require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise WorkflowError(f"kernel workflow requires Linux, not {sys.platform}")


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
) -> str:
    if repo.exists():
        raise WorkflowError(f"runner checkout must be fresh: {repo}")
    source = Path(linux_url).expanduser()
    clone_source = str(source.resolve()) if source.exists() else linux_url
    run_command(
        ["git", "clone", "--no-local", clone_source, str(repo)],
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




def run_workflow(
    ctx: RunContext,
    args: argparse.Namespace,
    series: dict[str, Any],
) -> dict[str, Any]:
    repo = ctx.work_dir / "repo"
    fixture_dir = Path(args.fixture_dir).expanduser().absolute()
    series_path = fixture_dir / DEFAULT_SERIES
    config_path = fixture_dir / DEFAULT_CONFIG

    base_commit_expression = series["base_commit"]
    commits = series["commits"]
    commit_ids = [commit["id"] for commit in commits]

    require_filesystem(repo, args.require_fstype, "Linux checkout")
    preparation_start = time.perf_counter_ns()
    prepare_linux_checkout(
        repo,
        linux_url=args.linux_url,
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

    # Pinned by CONFIG so timings stay comparable across machines.
    jobs = args.jobs

    steps: list[dict[str, Any]] = []
    cleanup_error: str | None = None
    cleanup_ms: int | None = None
    preparation_ms: int | None = None
    workflow_ms: int | None = None
    success = False
    error: str | None = None
    timed_start: int | None = None
    checkout_ready = False
    compiler_version: str | None = None

    try:
        begin_checkout_run(
            repo,
            base_commit=base_commit,
        )
        checkout_ready = True
        preparation_ms = elapsed_ms(preparation_start)
        print(
            f"Linux preparation complete ({preparation_ms} ms).",
            file=sys.stderr,
        )
        timed_start = time.perf_counter_ns()

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
            verify_tree(repo, commit_id)
            steps.append({
                "kind": "verify", "name": "tree-verify", "commit": commit_id,
                "ms": elapsed_ms(verify_start),
            })

        success = True
    except WorkflowError as exc:
        error = str(exc)
    except KeyboardInterrupt:
        error = "interrupted"
    except Exception as exc:
        error = f"unexpected error: {exc}"
        if args.verbose:
            traceback.print_exc()
    finally:
        if preparation_ms is None:
            preparation_ms = elapsed_ms(preparation_start)
        timed_end = time.perf_counter_ns()
        if timed_start is not None:
            workflow_ms = (timed_end - timed_start) // 1_000_000
        else:
            workflow_ms = 0
        if success:
            try:
                compiler_version = read_compiler_version(repo)
            except (OSError, RuntimeError) as exc:
                success = False
                error = str(exc)
        cleanup_start = time.perf_counter_ns()
        if checkout_ready:
            try:
                cleanup_checkout(
                    repo,
                    base_commit=base_commit,
                    verbose=args.verbose,
                )
            except WorkflowError as exc:
                cleanup_error = str(exc)
        cleanup_ms = elapsed_ms(cleanup_start)

    if cleanup_error:
        success = False
        error = (
            f"{error}; {cleanup_error}"
            if error
            else cleanup_error
        )

    unattributed_ms = max(0, (workflow_ms or 0) - sum(
        step["ms"] for step in steps
    ))

    return {
        "success": success,
        "error": error,
        "base_commit": base_commit,
        "compiler_version": compiler_version,
        "elapsed_ms": workflow_ms,
        "preparation_ms": preparation_ms,
        "cleanup_ms": cleanup_ms,
        "unattributed_ms": unattributed_ms,
        "steps": steps,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run dev-workflow directly in one disposable Linux checkout on the "
            "filesystem under test."
        )
    )
    parser.parse_args(argv)
    return argparse.Namespace(**CONFIG)


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    require_linux()
    ensure_dependencies(require_fstype=bool(args.require_fstype))
    fixture_dir = Path(args.fixture_dir).expanduser().absolute()
    series = validate_fixtures(fixture_dir)
    names = {DEFAULT_SERIES, DEFAULT_CONFIG}
    for commit in series["commits"]:
        names.update(commit[key] for key in ("search_commands", "read_commands", "edit_commands"))
    ctx.info.update({
        "jobs": args.jobs,
        "fixture_sha256": fixture_hashes(fixture_dir, names),
        "tool_versions": tool_versions(ctx.work_dir),
    })
    result = run_workflow(ctx, args, series)
    ctx.info["base_commit"] = result.pop("base_commit")
    compiler = result.pop("compiler_version")
    if compiler is not None:
        ctx.info["tool_versions"]["compiler"] = compiler
    if not result.pop("success"):
        raise WorkflowError(result["error"] or "workflow failed")
    result.pop("error")
    ctx.emit("workflow", {}, {key: result.pop(key) for key in (
        "elapsed_ms", "preparation_ms", "cleanup_ms", "unattributed_ms"
    )})
    for step, entry in enumerate(result.pop("steps"), 1):
        ctx.emit("steps", {
            "step": step, "name": entry["name"],
            "kind": entry["kind"], "commit": entry["commit"],
        }, {"ms": entry["ms"]})
