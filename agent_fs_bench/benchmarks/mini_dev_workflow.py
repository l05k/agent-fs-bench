"""Replay the tiny C fixture in a real disposable Git worktree.

The source repository is cloned from the committed bundle beside the worktree
on the target filesystem. Worktree prune/add and the full command/build/commit
sequence are timed; source setup and worktree removal are separate. No checkpoints
run. As upstream, commit trees are not compared.
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

from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .build_settings import read_compiler_version

DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "data" / "mini-dev-workflow"
DEFAULT_SERIES = "series.json"
DEFAULT_BUNDLE = "source-repo.bundle"

# Git metadata (objects, index, refs, logs) and the replayed tree must live on
# the filesystem under test, so the source repository sits next to the worktree.
SOURCE_REPO_SUFFIX = "-source"

# Keep this harness's configured parallelism; upstream invokes plain make.
# Change CONFIG["jobs"] only for a deliberate experiment; it is capped below.
FIXED_JOBS = 2

# ---------------------------------------------------------------------------
# Experiment configuration. These constants replace the former workload CLI
# flags; edit them in source for a deliberate experiment.
# ---------------------------------------------------------------------------
CONFIG = {
    "fixture_dir": str(DEFAULT_FIXTURE_DIR),
    "require_fstype": None,
    "jobs": FIXED_JOBS,
    "verbose": False,
}

CORE_COMMANDS = ("git", "bash", "make", "grep", "sed", "python3")



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
    try:
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
    except OSError as exc:
        raise WorkflowError(f"{what}: cannot start {argv[0]}: {exc}") from exc

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
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkflowError(f"cannot read fixture {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"cannot parse fixture {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"fixture {path} must contain a JSON object")
    return value


def load_commands(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkflowError(f"cannot read command file {path}: {exc}") from exc

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


def require_linux() -> None:
    if sys.platform.startswith("linux"):
        return
    raise WorkflowError(
        f"the mini-dev-workflow measures Linux filesystems and only runs on "
        f"Linux (current platform: {sys.platform})"
    )


def validate_fixtures(fixture_dir: Path) -> dict[str, Any]:
    """Fail fast on missing fixtures before the worktree is created."""
    if not fixture_dir.is_dir():
        raise WorkflowError(
            f"fixture directory not found: {fixture_dir}\n"
            "Deploy data/mini-dev-workflow with the package."
        )
    series_path = fixture_dir / DEFAULT_SERIES
    series = load_json(series_path)

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
        if not isinstance(commit.get("message"), str):
            raise WorkflowError(f"{series_path}: commit {commit['id']} has no message")
        files = commit.get("files")
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise WorkflowError(f"{series_path}: commit {commit['id']} has invalid files")
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
    return missing


def ensure_dependencies(*, require_fstype: bool = False) -> None:
    missing = missing_dependencies(require_fstype=require_fstype)
    if missing:
        raise WorkflowError("missing dependencies (install manually): " + ", ".join(missing))


def detect_filesystem(path: Path) -> str | None:
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return None
    proc = subprocess.run(
        [findmnt, "-T", str(path), "-n", "-o", "FSTYPE"],
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


def default_source_repo(worktree: Path) -> Path:
    """Return the source repository path next to the worktree.

    The repository must sit on the filesystem under test: ``git worktree add``
    keeps the index, refs and objects in the source repository, so anything
    placed elsewhere is timed against the wrong filesystem.
    """
    return worktree.parent / f"{worktree.name}{SOURCE_REPO_SUFFIX}"


def ensure_source_repo(source_repo: Path, fixture_dir: Path, *, verbose: bool) -> None:
    """Create a fresh source repository from the committed bundle."""
    if source_repo.exists():
        raise WorkflowError(f"runner source repository must be fresh: {source_repo}")
    bundle = fixture_dir / DEFAULT_BUNDLE
    if not bundle.is_file():
        raise WorkflowError(
            f"cannot create source repository {source_repo}: "
            f"bundle not found at {bundle}"
        )
    source_repo.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet",
         str(bundle), str(source_repo)],
        cwd=fixture_dir,
        what=f"extracting source repository from {bundle}",
        verbose=verbose,
    )


def prepare_source_repo(
    source_repo: Path, fixture_dir: Path, *, verbose: bool
) -> int:
    """Clone the source outside preparation/workload timing; return setup time."""
    start = time.perf_counter_ns()
    ensure_source_repo(source_repo, fixture_dir, verbose=verbose)
    return elapsed_ms(start)


def resolve_source_repo(source_repo: Path, *, verbose: bool) -> Path:
    """Validate the source repository and return the directory to run git in."""
    if not source_repo.exists():
        raise WorkflowError(
            f"source repository not found: {source_repo}\n"
            "It is created from the fixture bundle on first use; if that "
            "failed, remove the path and rerun."
        )
    if not source_repo.is_dir():
        raise WorkflowError(
            f"source repository must be a Git checkout, not a file: {source_repo}\n"
            f"If this is source-repo.bundle, extract it first: "
            f"git clone {source_repo} <dir>"
        )
    if not (source_repo / ".git").exists():
        raise WorkflowError(
            f"{source_repo} is not a Git repository; "
            "remove it and rerun so it is recreated from the bundle"
        )
    run_command(
        ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "--git-dir"],
        cwd=source_repo,
        what=f"opening source repository {source_repo}",
        verbose=verbose,
    )
    return source_repo




def remove_worktree(
    source_repo: Path,
    *,
    worktree: Path,
    verbose: bool,
) -> None:
    errors: list[str] = []
    if worktree.exists():
        try:
            run_git(
                source_repo,
                ["worktree", "remove", "--force", str(worktree)],
                what="removing the benchmark worktree",
                verbose=verbose,
            )
        except WorkflowError as exc:
            errors.append(str(exc))
    try:
        run_git(
            source_repo,
            ["worktree", "prune"],
            what="pruning stale worktree metadata",
            verbose=verbose,
        )
    except WorkflowError as exc:
        errors.append(str(exc))
    if errors:
        raise WorkflowError("worktree cleanup failed: " + "; ".join(errors))




def has_local_commit(repo: Path, revision: str) -> bool:
    # ``rev-parse --verify --quiet`` exits 1 for an unresolvable revision;
    # ``cat-file -e`` exits 128, which would bypass the friendly error below.
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
        what=f"checking for local commit {revision}",
        capture_stdout=True,
        allow_codes=(0, 1),
    )
    return result.stdout.strip() != ""




def resolve_jobs(args: argparse.Namespace) -> int:
    """Pin build parallelism.

    Keep this harness's cap to avoid scheduling noise. CONFIG["jobs"] is
    honoured up to that cap; the effective value is recorded in JSONL.
    """
    if type(args.jobs) is not int or args.jobs < 1:
        raise WorkflowError("jobs must be a positive integer")
    return min(args.jobs, FIXED_JOBS)


def run_workflow(ctx: RunContext, args: argparse.Namespace) -> dict[str, Any]:
    worktree = ctx.work_dir / "repo"
    fixture_dir = Path(args.fixture_dir).expanduser().absolute()
    source_repo = default_source_repo(worktree)

    series = validate_fixtures(fixture_dir)
    base_commit = series["base_commit"]
    commits = series["commits"]
    commit_ids = [commit["id"] for commit in commits]

    source_setup_ms = prepare_source_repo(
        source_repo, fixture_dir, verbose=args.verbose
    )
    source_repo = resolve_source_repo(source_repo, verbose=args.verbose)
    require_filesystem(source_repo, args.require_fstype, "source repository")

    preparation_start = time.perf_counter_ns()
    validate_source_repository(source_repo, base_commit=base_commit, commit_ids=commit_ids)

    jobs = resolve_jobs(args)
    steps: list[dict[str, Any]] = []
    cleanup_error: str | None = None
    cleanup_ms: int | None = None
    preparation_ms: int | None = None
    workflow_ms: int | None = None
    success = False
    error: str | None = None
    timed_start: int | None = None
    timed_end: int | None = None
    worktree_ready = False

    try:
        if worktree.exists():
            raise WorkflowError(f"runner worktree must be fresh: {worktree}")
        worktree_ready = True
        preparation_ms = elapsed_ms(preparation_start)
        timed_start = time.perf_counter_ns()

        prune_start = time.perf_counter_ns()
        run_git(
            source_repo,
            ["worktree", "prune"],
            what="pruning stale worktree metadata",
            verbose=args.verbose,
        )
        steps.append(
            {"step": "worktree: prune", "ms": elapsed_ms(prune_start)}
        )

        add_start = time.perf_counter_ns()
        run_git(
            source_repo,
            ["worktree", "add", "--detach", str(worktree), base_commit],
            what="creating the benchmark worktree",
            verbose=args.verbose,
        )
        steps.append(
            {"step": "worktree: add", "ms": elapsed_ms(add_start)}
        )


        initial = run_command(
            ["make", f"-j{jobs}"],
            cwd=worktree,
            what="initial build",
            verbose=args.verbose,
        )
        steps.append({"step": "initial-build: make", "ms": initial.ms})


        for commit in commits:
            commit_id = commit["id"]
            message = commit["message"]
            files = commit["files"]

            commands: list[tuple[str, str]] = []
            for command_file in ("search_commands", "read_commands", "edit_commands"):
                commands.append((command_file, commit[command_file]))

            for phase, filename in commands:
                category = phase.removesuffix("_commands")
                load_start = time.perf_counter_ns()
                loaded = load_commands(fixture_dir / filename)
                steps.append(
                    {
                        "step": f"{category}-load: {commit_id}",
                        "ms": elapsed_ms(load_start),
                    }
                )
                for idx, command in enumerate(loaded, 1):
                    result = run_shell(
                        command,
                        cwd=worktree,
                        what=f"{category} command for {commit_id} #{idx}",
                        verbose=args.verbose,
                    )
                    steps.append(
                        {"step": f"{category}: {commit_id} #{idx}", "ms": result.ms}
                    )

            incremental = run_command(
                ["make", f"-j{jobs}"],
                cwd=worktree,
                what=f"incremental build for {commit_id}",
                verbose=args.verbose,
            )
            steps.append(
                {"step": f"incremental-build: {commit_id}", "ms": incremental.ms}
            )


            status = run_git(
                worktree,
                ["status", "--short"],
                what=f"git status for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({"step": f"git-status: {commit_id}", "ms": status.ms})

            diff = run_git(
                worktree,
                ["diff", "--stat", "--", *files],
                what=f"git diff for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({"step": f"git-diff: {commit_id}", "ms": diff.ms})

            add = run_git(
                worktree,
                ["add", "--", *files],
                what=f"git add for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({"step": f"git-add: {commit_id}", "ms": add.ms})

            commit_result = run_git(
                worktree,
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
            steps.append({"step": f"git-commit: {commit_id}", "ms": commit_result.ms})


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
                ctx.parameters.setdefault("tool_versions", {})["compiler"] = (
                    read_compiler_version(worktree)
                )
            except (OSError, RuntimeError) as exc:
                success = False
                error = str(exc)
        cleanup_start = time.perf_counter_ns()
        if worktree_ready:
            try:
                remove_worktree(
                    source_repo,
                    worktree=worktree,
                    verbose=args.verbose,
                )
            except WorkflowError as exc:
                cleanup_error = str(exc)
        cleanup_ms = elapsed_ms(cleanup_start)

    if cleanup_error:
        success = False
        error = f"{error}; {cleanup_error}" if error else cleanup_error

    unattributed_ms = max(0, (workflow_ms or 0) - sum(
        step["ms"] for step in steps
    ))

    return {
        "success": success,
        "error": error,
        "base_commit": base_commit,
        "source_setup_ms": source_setup_ms,
        "preparation_ms": preparation_ms,
        "elapsed_ms": workflow_ms,
        "unattributed_ms": unattributed_ms,
        "cleanup_ms": cleanup_ms,
        "steps": steps,
    }


def validate_source_repository(
    source_repo: Path,
    *,
    base_commit: str,
    commit_ids: list[str],
) -> None:
    problems = [
        revision
        for revision in (base_commit, *commit_ids)
        if not has_local_commit(source_repo, revision)
    ]
    if problems:
        raise WorkflowError(
            f"source repository {source_repo} is missing fixture commits: "
            f"{', '.join(problems)}; remove that directory so it is recreated "
            "from the fixture bundle, or regenerate the fixture with "
            "tools/gen_mini_source_repo.py"
        )
    status = git_text(
        source_repo,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored"],
        what="checking source repository cleanliness",
    )
    if status:
        preview = "\n".join(status.splitlines()[:20])
        raise WorkflowError(
            "source repository must be clean before a run:\n" + preview
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the mini-dev-workflow in a disposable Git worktree on the "
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
    names = {DEFAULT_SERIES, DEFAULT_BUNDLE}
    for commit in series["commits"]:
        names.update(commit[key] for key in ("search_commands", "read_commands", "edit_commands"))
    hashes = {}
    for name in sorted(names):
        with (fixture_dir / name).open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    tools = {}
    for name in ("git", "make"):
        tools[name] = run_command(
            [name, "--version"], cwd=ctx.work_dir,
            what=f"reading {name} version", capture_stdout=True,
        ).stdout.strip().splitlines()[0]
    ctx.parameters = {
        "jobs": resolve_jobs(args),
        "fixture_sha256": hashes,
        "tool_versions": tools,
    }
    result = run_workflow(ctx, args)
    ctx.parameters["base_commit"] = result.pop("base_commit")
    if not result.pop("success"):
        raise WorkflowError(result["error"] or "workflow failed")
    result.pop("error")
    metrics = {key: result.pop(key) for key in (
        "elapsed_ms", "preparation_ms", "cleanup_ms", "unattributed_ms", "source_setup_ms"
    )}
    ctx.emit("workflow", metrics, details=result)
