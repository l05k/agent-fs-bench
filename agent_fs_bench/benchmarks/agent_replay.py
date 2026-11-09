"""Replay accepted Harbor agent traces against fresh task-data mounts on Linux."""

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

from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext

CONFIG = {
    "cases_file": PROJECT_ROOT / "local" / "agent-replay.toml",
    "repeats": 5,
}
REPLAY_ROOT = PROJECT_ROOT / "third_party" / "agent-mitm-replay"

FIGURES = [
    Figure("replays", "case", "elapsed_s", kind="box",
           xlabel="Replay case", ylabel="Wall-clock time (s)", name="replay-time"),
    Figure("phases", "phase", "seconds", kind="box", split=("case",),
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
    # Request metadata preservation; run() checks the restored input before replay.
    # A new destination per measurement prevents reuse of a mutated task tree.
    process = subprocess.run(
        ["cp", "-a", "--reflink=never", "--", str(source), str(destination)],
        capture_output=True, text=True,
    )
    if process.returncode:
        raise RuntimeError(f"cannot restore initial data: {process.stderr.strip()}")


def check_storage(case: ReplayCase, target: Path) -> None:
    for path in (REPLAY_ROOT, case.task, case.cassette,
                 *(mount.source for mount in case.mounts)):
        if path.is_relative_to(target) or target.is_relative_to(path):
            raise ValueError(f"replay inputs and tools must be outside target: {path}")


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    if sys.platform != "linux":
        raise RuntimeError("agent-replay measurements require Linux and a local Docker daemon")
    cases = load_cases(Path(args.cases_file).expanduser().resolve())
    executable = REPLAY_ROOT / ".venv" / "bin" / "agent-mitm-replay"
    if not executable.is_file():
        raise RuntimeError("initialize the submodule, then run uv sync --locked "
                           "--project third_party/agent-mitm-replay on the measurement host")
    for case in cases:
        check_storage(case, ctx.target.path.resolve())
    version_command = [str(executable.with_name("python")), "-c",
                       "import importlib.metadata as m, json, platform; "
                       "print(json.dumps({'python': platform.python_version(), "
                       "'harbor': m.version('harbor'), 'mitmproxy': m.version('mitmproxy')}))"]
    versions = subprocess.run(version_command, capture_output=True, text=True)
    if versions.returncode:
        raise RuntimeError(f"cannot inspect replay environment: {versions.stderr.strip()}")
    tool_versions = json.loads(versions.stdout)
    replay_paths = [REPLAY_ROOT / name for name in
                    ("pyproject.toml", "uv.lock", "uv.toml", ".python-version")]
    replay_paths += sorted((REPLAY_ROOT / "src").rglob("*.py"))
    replay_digest = fingerprint(REPLAY_ROOT, replay_paths)
    ctx.info["replay"] = {"tool_versions": tool_versions, "source_sha256": replay_digest}
    ctx.info["cases"] = {}
    for case in cases:
        cassette_paths = [case.cassette / name for name in
                          ("flows.mitm", "manifest.json", "commands.json", "task.toml", "validation.json")]
        environment = case.cassette / "environment"
        if environment.exists():
            cassette_paths += [environment, *environment.rglob("*")]
        manifest = json.loads((case.cassette / "manifest.json").read_text())
        expected = {mount.container: fingerprint(mount.source) for mount in case.mounts}
        ctx.info["cases"][case.name] = {
            "model": manifest["model"],
            "task_sha256": fingerprint(case.task),
            "cassette_sha256": fingerprint(case.cassette, cassette_paths),
            "mounts": [{"container": mount.container, "initial_sha256": expected[mount.container]}
                       for mount in case.mounts],
        }
        work = ctx.work_dir / case.name
        results = ctx.result_dir / case.name
        work.mkdir()
        results.mkdir()
        for repeat in range(1, args.repeats + 1):
            directory = work / f"repeat-{repeat}"
            directory.mkdir()
            output = results / f"repeat-{repeat}"
            command = [str(executable), "replay", "--task", str(case.task),
                       "--cassette", str(case.cassette), "--output", str(output)]
            for index, mount in enumerate(case.mounts):
                destination = directory / f"mount-{index}"
                restore(mount.source, destination)
                if fingerprint(destination) != expected[mount.container]:
                    raise RuntimeError(f"restored task data differs from source: {destination}")
                command += ["--mount", f"{destination}:{mount.container}"]
            log = results / f"repeat-{repeat}.log"
            with log.open("x") as handle:
                process = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
            if process.returncode:
                raise RuntimeError(f"agent replay failed; inspect {log} and {output}")
            summary = json.loads((output / "summary.json").read_text())
            if summary["mode"] != "replay" or summary["checked"] or not summary["measured"]:
                raise ValueError(f"not a measured replay: {output}")
            ctx.emit("replays", {"case": case.name, "repeat": repeat},
                     {"elapsed_s": summary["elapsed_sec"],
                      "model_requests": summary["model_requests"]})
            for phase, seconds in summary["phase_seconds"].items():
                if seconds is not None:
                    ctx.emit("phases", {"case": case.name, "repeat": repeat, "phase": phase},
                             {"seconds": seconds})
