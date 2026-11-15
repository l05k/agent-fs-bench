"""Replay a pinned Harbor task cohort and record every planned outcome."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
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
    # None runs the whole cohort; a case name selects a development run.
    "case": None,
    "repeats": 5,
    "tasks": 1,
}
REPLAY_ROOT = PROJECT_ROOT / "third_party" / "agent-mitm-replay"

FIGURES = [
    Figure("replays", "tasks", "tool_s", kind="box", split=("case",),
           xlabel="Concurrent agents", ylabel="Tool wall time (s)", name="replay-tool-time"),
]

@dataclass(frozen=True)
class Mount:
    source: Path
    container: str

@dataclass(frozen=True)
class ReplayCase:
    name: str
    benchmark: str
    task_id: str
    agent: str
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
    keys = set()
    for entry in entries:
        name = entry["name"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in names:
            raise ValueError(f"invalid or duplicate replay case name: {name!r}")
        names.add(name)
        key = (entry["benchmark"], entry["task_id"], entry["agent"])
        if not all(key) or key[2] not in ("codex", "openclaw") or key in keys:
            raise ValueError(f"invalid or duplicate benchmark/task/harness: {key}")
        keys.add(key)
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
        case = ReplayCase(name, *key, local_path(entry["task"]),
                          local_path(entry["cassette"]), mounts)
        cases.append(case)
    if not cases:
        raise ValueError("at least one replay case is required")
    return cases

def select_case(cases: list[ReplayCase], name: str) -> ReplayCase:
    """Select one case for a development run."""
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
        for path in (case.task, *(mount.source for mount in case.mounts)):
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
    replay_paths += sorted(path for path in (REPLAY_ROOT / "src").rglob("*")
                           if path.suffix in {".py", ".mjs"})
    facts = {"replay": {"tool_versions": tool_versions,
                        "source_sha256": fingerprint(REPLAY_ROOT, replay_paths)},
             "cases": {}}
    for case in cases:
        facts["cases"][case.name] = {
            "benchmark": case.benchmark, "task_id": case.task_id, "agent": case.agent,
            "task_sha256": fingerprint(case.task),
            "mounts": [{"container": mount.container, "initial_sha256": fingerprint(mount.source)}
                       for mount in case.mounts],
        }
        if (case.cassette / "manifest.json").is_file():
            paths = [case.cassette / name for name in ("flows.mitm", "manifest.json", "tools.json")]
            manifest = json.loads(paths[1].read_text())
            if manifest["schema_version"] != 3 or manifest["agent"] != case.agent:
                raise ValueError(f"record a native {case.agent} cassette for {case.name}")
            facts["cases"][case.name].update(model=manifest["model"],
                                             agent_version=manifest["agent_version"],
                                             cassette_sha256=fingerprint(case.cassette, paths))
    return facts

def measurements(summary: dict, output: Path) -> dict:
    """Read one classified trial; unsuccessful trials have no timing metrics."""
    if summary["mode"] != "replay" or summary["status"] not in {"ok", "timeout", "error", "divergence"}:
        raise ValueError(f"not a replay outcome: {output}")
    return summary

def prepare_replay(t: cluster.Task, case: ReplayCase, repeat: int,
                   expected: dict[str, str]) -> tuple[Path, Path, list[str]]:
    directory = t.task_dir / case.name / f"repeat-{repeat}"
    directory.mkdir(parents=True)
    diagnostics = t.log_dir / f"task-{t.local_index:04d}" / case.name
    diagnostics.mkdir(parents=True, exist_ok=True)
    output = diagnostics / f"repeat-{repeat}"
    command = [str(REPLAY_ROOT / ".venv" / "bin" / "agent-mitm-replay"), "replay",
               "--task", str(case.task), "--cassette", str(case.cassette), "--output", str(output)]
    for index, mount in enumerate(case.mounts):
        destination = directory / f"mount-{index}"
        restore_verified(mount.source, destination, expected[mount.container])
        command += ["--mount", f"{destination}:{mount.container}"]
    subprocess.run(["sync", "-f", "--", str(directory)], check=True)
    return directory, output, command


def replay_once(output: Path, command: list[str]) -> dict:
    log = output.with_suffix(".log")
    with log.open("x") as handle:
        process = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
    summary_path = output / "summary.json"
    if not summary_path.is_file():
        raise RuntimeError(f"Unclassified replay failure; inspect {log}")
    result = measurements(json.loads(summary_path.read_text()), output)
    if (process.returncode == 0) != (result["status"] == "ok"):
        raise RuntimeError(f"Replay exit status contradicts its summary: {output}")
    return result

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    if sys.platform != "linux":
        raise RuntimeError("agent-replay measurements require Linux and a local Docker daemon")
    if type(args.repeats) is not int or args.repeats < 1:
        raise ValueError(f"repeats must be a positive integer: {args.repeats!r}")
    cluster.run_tasks(ctx, args)

def task(t: cluster.Task) -> None:
    cases_file = cases_path(t.args.cases_file)
    cases = load_cases(cases_file)
    if t.args.case is not None:
        cases = [select_case(cases, t.args.case)]
    facts = node_facts(t, cases, cases_file) if t.leader else None
    facts = t.node_comm.bcast(facts, root=0)
    t.node_info(facts if t.leader else None)
    entries = t.gather({"node": t.node, "facts": facts} if t.leader else None)
    if t.index == 0:
        check_consistent([entry for entry in entries if entry is not None])
    hosts = t.gather(t.node)
    for case in cases:
        inputs = facts["cases"][case.name]
        expected = {mount["container"]: mount["initial_sha256"] for mount in inputs["mounts"]}
        for repeat in range(1, t.args.repeats + 1):
            if "cassette_sha256" not in inputs:
                results = [{"status": "capture_missing"}] * t.count
            else:
                directory, output, command = prepare_replay(t, case, repeat, expected)
                _, results = t.batch(replay_once, output, command)
                shutil.rmtree(directory)
            if t.index != 0:
                continue
            for sample, result in enumerate(results):
                keys = {"case": case.name, "benchmark": case.benchmark, "task_id": case.task_id,
                        "agent": case.agent, "tasks": t.count, "repeat": repeat,
                        "sample": sample, "host": hosts[sample]}
                t.emit("outcomes", {**keys, "status": result["status"]}, {"attempts": 1})
                if result["status"] != "ok":
                    continue
                if result["agent"] != case.agent:
                    raise ValueError(f"Wrong harness in result for {case.name}")
                t.emit("replays", keys, {key: result[key] for key in ("tool_s", "harbor_s", "model_requests")})
                for phase, seconds in result["phase_seconds"].items():
                    t.emit("phases", {**keys, "phase": phase}, {"seconds": seconds})
