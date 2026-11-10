"""Replay the tiny C fixture in a real disposable Git worktree per task.

Every task clones the committed bundle into its own source repository beside
its own worktree, then all tasks run the worktree/build/commit sequence between
barriers. Worktree prune/add and the full command/build/commit sequence are
timed; source setup, validation and worktree removal are separate. No
checkpoints run. As upstream, commit trees are not compared.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path
from typing import Any

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .build_settings import read_compiler_version
from .dev_workflow import (
    WorkflowError,
    check_build_parallelism,
    elapsed_ms,
    fixture_hashes,
    git_text,
    has_local_commit,
    load_commands,
    load_json,
    require_filesystem,
    resolve_fixture_dir,
    run_command,
    run_git,
    run_shell,
    tool_versions,
)

# Relative to each machine's checkout: no controller path may reach a task.
DEFAULT_FIXTURE_DIR = Path("data") / "mini-dev-workflow"
DEFAULT_SERIES = "series.json"
DEFAULT_BUNDLE = "source-repo.bundle"

# Git metadata (objects, index, refs, logs) and the replayed tree must live on
# the filesystem under test, so the source repository sits next to the worktree.
SOURCE_REPO_SUFFIX = "-source"

# Keep this harness's configured parallelism; upstream invokes plain make.
# Both "jobs" and "cpus_per_task" are capped here for a deliberate experiment.
FIXED_JOBS = 2

WORKFLOW_METRICS = (
    "elapsed_ms", "preparation_ms", "cleanup_ms", "unattributed_ms", "source_setup_ms",
)

# ---------------------------------------------------------------------------
# Experiment configuration. These constants replace the former workload CLI
# flags; edit them in source for a deliberate experiment.
# ---------------------------------------------------------------------------
CONFIG = {
    "fixture_dir": str(DEFAULT_FIXTURE_DIR),
    "require_fstype": None,
    "tasks": 1,
    # Must equal "jobs": one pinned CPU per make job.
    "cpus_per_task": FIXED_JOBS,
    "jobs": FIXED_JOBS,
    "oversubscribe": 1,
    "verbose": False,
}

FIGURES = [
    Figure("steps", "kind", "ms", kind="box", xlabel="Step kind",
           ylabel="Duration (s)", yscale=1000, split=("tasks",), name="step-duration-tasks"),
    Figure("workflow", "tasks", "elapsed_ms", kind="box", xlabel="Tasks",
           ylabel="Workflow (s)", yscale=1000, name="workflow-duration"),
]

CORE_COMMANDS = ("git", "bash", "make", "grep", "sed", "python3")

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

def fixture_names(series: dict[str, Any]) -> set[str]:
    """Fixture files whose hashes identify the pinned series and bundle."""
    names = {DEFAULT_SERIES, DEFAULT_BUNDLE}
    for commit in series["commits"]:
        names.update(commit[key] for key in
                     ("search_commands", "read_commands", "edit_commands"))
    return names

def missing_dependencies(*, require_fstype: bool = False) -> list[str]:
    missing = [name for name in CORE_COMMANDS if shutil.which(name) is None]
    if require_fstype and shutil.which("findmnt") is None:
        missing.append("findmnt")
    return missing

def ensure_dependencies(*, require_fstype: bool = False) -> None:
    missing = missing_dependencies(require_fstype=require_fstype)
    if missing:
        raise WorkflowError("missing dependencies (install manually): " + ", ".join(missing))

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

def resolve_jobs(jobs: int) -> int:
    """Cap build parallelism so runs stay comparable."""
    return min(jobs, FIXED_JOBS)

def prepare_worktree(
    task_dir: Path,
    *,
    fixture_dir: Path,
    args: argparse.Namespace,
    series: dict[str, Any],
) -> tuple[Path, Path, int, int]:
    """Create and validate this task's source repository; never timed."""
    worktree = task_dir / "repo"
    source_repo = default_source_repo(worktree)
    require_filesystem(source_repo, args.require_fstype, "source repository")
    source_setup_ms = prepare_source_repo(
        source_repo, fixture_dir, verbose=args.verbose
    )
    source_repo = resolve_source_repo(source_repo, verbose=args.verbose)

    preparation_start = time.perf_counter_ns()
    validate_source_repository(
        source_repo,
        base_commit=series["base_commit"],
        commit_ids=[commit["id"] for commit in series["commits"]],
    )
    if worktree.exists():
        raise WorkflowError(f"runner worktree must be fresh: {worktree}")
    return worktree, source_repo, source_setup_ms, elapsed_ms(preparation_start)

def run_workflow(
    worktree: Path,
    source_repo: Path,
    *,
    args: argparse.Namespace,
    fixture_dir: Path,
    series: dict[str, Any],
    preparation_ms: int,
    source_setup_ms: int,
) -> dict[str, Any]:
    """Run the timed workflow in `worktree`, then remove it; raise on failure."""
    base_commit = series["base_commit"]
    commits = series["commits"]
    jobs = args.jobs

    steps: list[dict[str, Any]] = []
    timed_start = time.perf_counter_ns()
    try:
        prune_start = time.perf_counter_ns()
        run_git(
            source_repo,
            ["worktree", "prune"],
            what="pruning stale worktree metadata",
            verbose=args.verbose,
        )
        steps.append({
            "kind": "worktree", "name": "prune", "commit": "",
            "ms": elapsed_ms(prune_start),
        })

        add_start = time.perf_counter_ns()
        run_git(
            source_repo,
            ["worktree", "add", "--detach", str(worktree), base_commit],
            what="creating the benchmark worktree",
            verbose=args.verbose,
        )
        steps.append({
            "kind": "worktree", "name": "add", "commit": "",
            "ms": elapsed_ms(add_start),
        })

        initial = run_command(
            ["make", f"-j{jobs}"],
            cwd=worktree,
            what="initial build",
            verbose=args.verbose,
        )
        steps.append({
            "kind": "build", "name": "initial-build", "commit": "", "ms": initial.ms,
        })

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
                steps.append({
                    "kind": "command", "name": f"{category}-load",
                    "commit": commit_id, "ms": elapsed_ms(load_start),
                })
                for command in loaded:
                    result = run_shell(
                        command,
                        cwd=worktree,
                        what=f"{category} command for {commit_id}",
                        verbose=args.verbose,
                    )
                    steps.append({
                        "kind": "command", "name": category,
                        "commit": commit_id, "ms": result.ms,
                    })

            incremental = run_command(
                ["make", f"-j{jobs}"],
                cwd=worktree,
                what=f"incremental build for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "build", "name": "incremental-build",
                "commit": commit_id, "ms": incremental.ms,
            })

            status = run_git(
                worktree,
                ["status", "--short"],
                what=f"git status for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "status", "commit": commit_id, "ms": status.ms,
            })

            diff = run_git(
                worktree,
                ["diff", "--stat", "--", *files],
                what=f"git diff for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "diff", "commit": commit_id, "ms": diff.ms,
            })

            add = run_git(
                worktree,
                ["add", "--", *files],
                what=f"git add for {commit_id}",
                verbose=args.verbose,
            )
            steps.append({
                "kind": "git", "name": "add", "commit": commit_id, "ms": add.ms,
            })

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
            steps.append({
                "kind": "git", "name": "commit", "commit": commit_id,
                "ms": commit_result.ms,
            })

        workflow_ms = elapsed_ms(timed_start)
        compiler_version = read_compiler_version(worktree)
    finally:
        cleanup_start = time.perf_counter_ns()
        remove_worktree(source_repo, worktree=worktree, verbose=args.verbose)
        cleanup_ms = elapsed_ms(cleanup_start)

    unattributed_ms = max(0, workflow_ms - sum(step["ms"] for step in steps))

    return {
        "base_commit": base_commit,
        "compiler_version": compiler_version,
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
            "Replay the mini workflow in one disposable Git worktree per task "
            "on the filesystem under test."
        )
    )
    parser.parse_args(argv)
    args = argparse.Namespace(**CONFIG)
    args.jobs = resolve_jobs(args.jobs)
    args.cpus_per_task = resolve_jobs(args.cpus_per_task)
    check_build_parallelism(args.jobs, args.cpus_per_task)
    return args

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    cluster.run_tasks(ctx, args)

def task(t: cluster.Task) -> None:
    """Clone the fixture bundle and run one mini workflow instance here."""
    args = t.args
    fixture_dir = resolve_fixture_dir(args.fixture_dir)
    series = validate_fixtures(fixture_dir)
    if t.leader:
        ensure_dependencies(require_fstype=bool(args.require_fstype))
        versions = tool_versions(PROJECT_ROOT)
    else:
        versions = None
    t.node_barrier()

    worktree, source_repo, source_setup_ms, preparation_ms = prepare_worktree(
        t.task_dir,
        fixture_dir=fixture_dir,
        args=args,
        series=series,
    )
    t.log(f"task {t.index} on {t.node}: worktree ready in {preparation_ms} ms")

    t.barrier()
    started = time.perf_counter_ns()
    summary = run_workflow(
        worktree,
        source_repo,
        args=args,
        fixture_dir=fixture_dir,
        series=series,
        preparation_ms=preparation_ms,
        source_setup_ms=source_setup_ms,
    )
    t.barrier()
    batch_ms = elapsed_ms(started)

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
        "batch_ms": batch_ms,
        "base_commit": first["base_commit"],
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
