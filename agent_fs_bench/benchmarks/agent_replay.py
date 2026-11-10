"""Replay accepted Harbor agent traces against fresh task-data mounts on Linux.

One run replays one case (`CONFIG['case']`) at one task count. Each task runs its own
independent replay of every repeat on its own machine: it restores its own initial-data
copies under its task directory and starts together with every other task through one
global Open MPI job. Replay output and logs are diagnostics and stay in this node's
checkout.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext

CONFIG = {
    # Relative names resolve in each machine's own checkout, never on the controller.
    "cases_file": "local/agent-replay.toml",
    # One case per run; None selects the only case of the cases file.
    "case": None,
    "repeats": 5,
    "tasks": 1,
}
REPLAY_ROOT = PROJECT_ROOT / "third_party" / "agent-mitm-replay"

FIGURES = [
    Figure("replays", "tasks", "elapsed_s", kind="box", split=("case",),
           xlabel="Tasks", ylabel="Wall-clock time (s)", name="replay-time-tasks"),
    Figure("phases", "phase", "seconds", kind="box", split=("case", "tasks"),
           xlabel="Harbor phase", ylabel="Phase time (s)", name="replay-phases"),
]

@dataclass(frozen=True)
class Mount:
    source: Path
    container: str

@dataclass(frozen=True)
class ReplayCase:
    name: str
    task: Path
    cassette: Path
    mounts: tuple[Mount, ...]

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return argparse.Namespace(**CONFIG)

def cases_path(value: str) -> Path:
    """The cases file in this machine's checkout; a relative name resolves there."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path

def load_cases(config: Path) -> list[ReplayCase]:
    config = config.expanduser().resolve()
    with config.open("rb") as handle:
        entries = tomllib.load(handle)["cases"]

    def local_path(value: str) -> Path:
        return (config.parent / Path(value).expanduser()).resolve()

    cases = []
    names = set()
    for entry in entries:
        name = entry["name"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in names:
            raise ValueError(f"invalid or duplicate replay case name: {name!r}")
        names.add(name)
        mounts = tuple(Mount(local_path(mount["source"]), mount["container"])
                       for mount in entry["mounts"])
        destinations = []
        for mount in mounts:
            destination = PurePosixPath(mount.container)
            if (not destination.is_absolute() or destination == PurePosixPath("/")
                    or ".." in destination.parts or ":" in mount.container):
                raise ValueError(f"invalid container mount: {mount.container}")
            if any(destination.is_relative_to(other) or other.is_relative_to(destination)
                   for other in destinations):
                raise ValueError(f"overlapping container mounts in {name}")
            destinations.append(destination)
        if not mounts:
            raise ValueError(f"replay case {name} needs at least one task-data mount")
        case = ReplayCase(name, local_path(entry["task"]),
                          local_path(entry["cassette"]), mounts)
        if not (case.cassette / "validation.json").is_file():
            raise ValueError(f"validate cassette once before benchmarking: {case.cassette}")
        cases.append(case)
    if not cases:
        raise ValueError("at least one replay case is required")
    return cases

def select_case(cases: list[ReplayCase], name: str | None) -> ReplayCase:
    """The one case this run replays."""
    if name is None:
        if len(cases) != 1:
            raise ValueError("the cases file lists several cases; set CONFIG['case'] to one of "
                             f"{', '.join(case.name for case in cases)}")
        return cases[0]
    for case in cases:
        if case.name == name:
            return case
    raise ValueError(f"unknown replay case {name!r}; cases: "
                     f"{', '.join(case.name for case in cases)}")

def fingerprint(root: Path, paths: list[Path] | None = None) -> str:
    """Hash input bytes, modes, symlinks and hard-link topology, not host paths."""
    digest = hashlib.sha256()
    links = {}
    if paths is None:
        paths = [root, *root.rglob("*")]
    for path in sorted(paths):
        metadata = path.lstat()
        relative = str(path.relative_to(root))
        mode = metadata.st_mode
        digest.update(json.dumps([relative, mode], sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False).encode() + b"\0")
        if stat.S_ISLNK(mode):
            digest.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(mode):
            inode = (metadata.st_dev, metadata.st_ino)
            if inode in links:
                digest.update(b"hardlink\0" + links[inode].encode())
            else:
                links[inode] = relative
                with path.open("rb") as handle:
                    digest.update(hashlib.file_digest(handle, "sha256").digest())
        elif not stat.S_ISDIR(mode):
            raise ValueError(f"unsupported input file type: {path}")
        digest.update(b"\0")
    return digest.hexdigest()

def restore(source: Path, destination: Path) -> None:
    process = subprocess.run(
        ["cp", "-a", "--reflink=never", "--", str(source), str(destination)],
        capture_output=True, text=True,
    )
    if process.returncode:
        raise RuntimeError(f"cannot restore initial data: {process.stderr.strip()}")

def restore_verified(source: Path, destination: Path, expected: str) -> None:
    """Restore one task's private copy and reject any metadata or content loss.

    A new destination per measurement prevents reuse of a mutated task tree.
    """
    restore(source, destination)
    if fingerprint(destination) != expected:
        raise RuntimeError(f"restored task data differs from source: {destination}")

def check_storage(case: ReplayCase, target: Path, cases_file: Path) -> None:
    """Inputs, tools and the cases file must stay outside the measured target."""
    target = target.resolve()
    for path in (cases_file, REPLAY_ROOT, case.task, case.cassette,
                 *(mount.source for mount in case.mounts)):
        path = path.resolve()
        if path.is_relative_to(target) or target.is_relative_to(path):
            raise ValueError(f"replay inputs and tools must be outside target: {path}")

def check_consistent(entries: list[dict]) -> None:
    """Every machine must fingerprint the same tools and inputs."""
    for entry in entries[1:]:
        if entry["facts"] != entries[0]["facts"]:
            raise ValueError("replay tools or inputs differ between machines: "
                             f"{entries[0]['node']} and {entry['node']}")

def node_facts(t: cluster.Task, cases: list[ReplayCase], cases_file: Path) -> dict:
    """Check this machine once and fingerprint every input its tasks reuse."""
    executable = REPLAY_ROOT / ".venv" / "bin" / "agent-mitm-replay"
    for case in cases:
        check_storage(case, t.target.path, cases_file)
        for path in (case.task, case.cassette, *(mount.source for mount in case.mounts)):
            if not path.exists():
                raise RuntimeError(f"{t.node}: replay input is missing on this machine: {path}")
    if not executable.is_file():
        raise RuntimeError(f"{t.node}: initialize the submodule, then run uv sync --locked "
                           f"--project {REPLAY_ROOT}")
    version_command = [str(executable.with_name("python")), "-c",
                       "import importlib.metadata as m, json, platform; "
                       "print(json.dumps({'python': platform.python_version(), "
                       "'harbor': m.version('harbor'), 'mitmproxy': m.version('mitmproxy')}))"]
    versions = subprocess.run(version_command, capture_output=True, text=True)
    if versions.returncode:
        raise RuntimeError(f"{t.node}: cannot inspect replay environment: "
                           f"{versions.stderr.strip()}")
    tool_versions = json.loads(versions.stdout)
    replay_paths = [REPLAY_ROOT / name for name in
                    ("pyproject.toml", "uv.lock", "uv.toml", ".python-version")]
    replay_paths += sorted((REPLAY_ROOT / "src").rglob("*.py"))
    facts = {"replay": {"tool_versions": tool_versions,
                        "source_sha256": fingerprint(REPLAY_ROOT, replay_paths)},
             "cases": {}}
    for case in cases:
        cassette_paths = [case.cassette / name for name in
                          ("flows.mitm", "manifest.json", "commands.json", "task.toml",
                           "validation.json")]
        environment = case.cassette / "environment"
        if environment.exists():
            cassette_paths += [environment, *environment.rglob("*")]
        manifest = json.loads((case.cassette / "manifest.json").read_text())
        facts["cases"][case.name] = {
            "model": manifest["model"],
            "task_sha256": fingerprint(case.task),
            "cassette_sha256": fingerprint(case.cassette, cassette_paths),
            "mounts": [{"container": mount.container,
                        "initial_sha256": fingerprint(mount.source)}
                       for mount in case.mounts],
        }
    return facts

def measurements(summary: dict, output: Path) -> dict:
    """The measured values of one replay summary; reject record/validate output."""
    if summary["mode"] != "replay" or summary["checked"] or not summary["measured"]:
        raise ValueError(f"not a measured replay: {output}")
    return {"elapsed_s": summary["elapsed_sec"],
            "model_requests": summary["model_requests"],
            "phases": {phase: seconds for phase, seconds in summary["phase_seconds"].items()
                       if seconds is not None}}

def replay_once(t: cluster.Task, case: ReplayCase, repeat: int,
                expected: dict[str, str]) -> dict:
    """One task's independent replay of one case and repeat."""
    directory = t.task_dir / case.name / f"repeat-{repeat}"
    directory.mkdir(parents=True)
    diagnostics = t.log_dir / f"task-{t.local_index:04d}" / case.name
    diagnostics.mkdir(parents=True, exist_ok=True)
    output = diagnostics / f"repeat-{repeat}"
    log = diagnostics / f"repeat-{repeat}.log"
    command = [str(REPLAY_ROOT / ".venv" / "bin" / "agent-mitm-replay"), "replay",
               "--task", str(case.task), "--cassette", str(case.cassette),
               "--output", str(output)]
    for index, mount in enumerate(case.mounts):
        destination = directory / f"mount-{index}"
        restore_verified(mount.source, destination, expected[mount.container])
        command += ["--mount", f"{destination}:{mount.container}"]
    with log.open("x") as handle:
        process = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
    if process.returncode:
        raise RuntimeError(f"{t.node}: agent replay failed; inspect {log} and {output}")
    summary = json.loads((output / "summary.json").read_text())
    return measurements(summary, output)

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    if sys.platform != "linux":
        raise RuntimeError("agent-replay measurements require Linux and a local Docker daemon")
    if type(args.repeats) is not int or args.repeats < 1:
        raise ValueError(f"repeats must be a positive integer: {args.repeats!r}")
    cluster.run_tasks(ctx, args)

def task(t: cluster.Task) -> None:
    cases_file = cases_path(t.args.cases_file)
    case = select_case(load_cases(cases_file), t.args.case)
    facts = node_facts(t, [case], cases_file) if t.leader else None
    facts = t.node_comm.bcast(facts, root=0)
    t.node_info(facts if t.leader else None)
    t.node_barrier()
    entries = t.gather({"node": t.node, "facts": facts} if t.leader else None)
    if t.index == 0:
        check_consistent([entry for entry in entries if entry is not None])
    hosts = t.gather(t.node)
    expected = {mount["container"]: mount["initial_sha256"]
                for mount in facts["cases"][case.name]["mounts"]}
    for repeat in range(1, t.args.repeats + 1):
        wall, results = t.batch(replay_once, t, case, repeat, expected)
        if t.index != 0:
            continue
        for sample, result in enumerate(results):
            keys = {"case": case.name, "tasks": t.count, "repeat": repeat,
                    "sample": sample, "host": hosts[sample]}
            t.emit("replays", keys,
                   {"elapsed_s": result["elapsed_s"],
                    "model_requests": result["model_requests"]})
            for phase, seconds in result["phases"].items():
                t.emit("phases", {**keys, "phase": phase}, {"seconds": seconds})
        t.log(f"[{t.target.name}/{case.name}] repeat {repeat}: {t.count} task(s), "
              f"wall={wall:.3f}s")
