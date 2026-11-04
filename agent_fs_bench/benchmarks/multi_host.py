"""Concurrent SSH workers on an externally mounted shared filesystem."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import tomllib
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from agent_fs_bench.results import PROJECT_ROOT, RunContext

REMOTE = True
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# ---------------------------------------------------------------------------
# Experiment configuration. These constants replace the former workload CLI
# flags; edit them in source for a deliberate experiment. The hosts file is
# private input and must never be committed.
# ---------------------------------------------------------------------------
CONFIG = {
    "hosts": PROJECT_ROOT / "local" / "hosts.toml",
    "files_per_host": 1000,
    "file_size_bytes": 4096,
    "directories": 32,
    "shared_files_per_host": 200,
    "rounds": 3,
    "fsync": True,
    "read_back": True,
    "cleanup_private": False,
    "worker_dir": Path("/var/tmp/agent-fs-bench"),
    "connect_timeout_s": 15,
    "command_timeout_s": 120,
    "ready_timeout_s": 180,
    "run_timeout_s": 1800,
    "keep_remote": False,
}


class RemoteError(RuntimeError):
    """An SSH command failed or a remote host violated the protocol."""


class WorkerError(RuntimeError):
    """The remote workload failed."""


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    user: str | None = None
    port: int = 22
    ssh_args: tuple[str, ...] = ()
    python: str = "python3"


@dataclass(frozen=True)
class WorkloadConfig:
    files_per_host: int
    file_size_bytes: int
    directories: int
    shared_files_per_host: int
    rounds: int
    fsync: bool
    read_back: bool
    cleanup_private: bool


@dataclass(frozen=True)
class ExperimentPlan:
    hosts: tuple[Host, ...]
    workload: WorkloadConfig
    target: Path
    run_root: Path
    remote_root: Path
    run_id: str
    token: str
    connect_timeout_s: int
    command_timeout_s: int
    ready_timeout_s: int
    run_timeout_s: int


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return argparse.Namespace(**CONFIG)


_POSITIVE_CONFIG_KEYS = (
    "files_per_host",
    "file_size_bytes",
    "directories",
    "shared_files_per_host",
    "rounds",
    "connect_timeout_s",
    "command_timeout_s",
    "ready_timeout_s",
    "run_timeout_s",
)
_BOOLEAN_CONFIG_KEYS = ("fsync", "read_back", "cleanup_private", "keep_remote")


def _validate_config(args: argparse.Namespace) -> None:
    for key in _POSITIVE_CONFIG_KEYS:
        value = getattr(args, key)
        if type(value) is not int or value < 1:
            raise ValueError(f"CONFIG[{key!r}] must be a positive integer")
    for key in _BOOLEAN_CONFIG_KEYS:
        if type(getattr(args, key)) is not bool:
            raise ValueError(f"CONFIG[{key!r}] must be a boolean")


def _load_hosts(path: Path) -> tuple[Host, ...]:
    try:
        with path.expanduser().open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ValueError(
            f"cannot read hosts file {path}: {exc}; copy "
            "experiments/hosts.example.toml to local/hosts.toml and edit the "
            "SSH host entries"
        ) from exc
    if set(document) != {"hosts"} or not isinstance(document["hosts"], list) or not document["hosts"]:
        raise ValueError("hosts TOML must contain a non-empty [[hosts]] list only")
    hosts = []
    names: set[str] = set()
    endpoints: set[tuple[str | None, str, int]] = set()
    for item in document["hosts"]:
        if not isinstance(item, dict) or set(item) - {"name", "address", "user", "port", "ssh_args", "python"}:
            raise ValueError("unknown host configuration keys")
        name, address = item.get("name"), item.get("address")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in names:
            raise ValueError("host names must be unique safe aliases")
        if not isinstance(address, str) or not address or address.startswith("-") or any(c.isspace() for c in address):
            raise ValueError(f"{name}: invalid SSH address")
        user = item.get("user")
        if user is not None and (not isinstance(user, str) or not NAME_RE.fullmatch(user)):
            raise ValueError(f"{name}: invalid SSH user")
        port = item.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"{name}: port must be in 1..65535")
        ssh_args = item.get("ssh_args", [])
        if not isinstance(ssh_args, list) or any(not isinstance(s, str) or not s for s in ssh_args):
            raise ValueError(f"{name}: ssh_args must be an array of non-empty strings")
        python = item.get("python", "python3")
        if not isinstance(python, str) or not python or "\x00" in python:
            raise ValueError(f"{name}: invalid Python executable")
        endpoint = (user, address, port)
        if endpoint in endpoints:
            raise ValueError(f"{name}: duplicate SSH endpoint")
        endpoints.add(endpoint)
        hosts.append(Host(name, address, user, port, tuple(ssh_args), python))
        names.add(name)
    return tuple(hosts)


def _safe_absolute(path: Path) -> None:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ValueError(f"expected an absolute non-root path without '..': {path}")




def _payload(host_name: str, size: int) -> bytes:
    seed = hashlib.blake2b(host_name.encode("utf-8"), digest_size=32).digest()
    repeats = (size + len(seed) - 1) // len(seed)
    return (seed * repeats)[:size]


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise WorkerError("short write while creating test file")
        view = view[written:]


def _create_file(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        _write_all(descriptor, data)
    finally:
        os.close(descriptor)


def _read_file(path: Path, expected_size: int) -> int:
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
    if total != expected_size:
        raise WorkerError(
            f"short read for {path}: expected {expected_size} bytes, got {total}"
        )
    return total


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _latency_summary(latencies_ns: Sequence[int]) -> dict[str, float]:
    if not latencies_ns:
        return {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
        }
    ordered = sorted(latencies_ns)

    def percentile(fraction: float) -> float:
        index = round((len(ordered) - 1) * fraction)
        return ordered[index] / 1_000_000

    return {
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": max(ordered) / 1_000_000,
        "mean_ms": statistics.fmean(ordered) / 1_000_000,
    }


def _phase_metric(
    round_number: int,
    phase: str,
    *,
    elapsed_s: float,
    latencies_ns: Sequence[int],
    bytes_count: int,
) -> dict[str, Any]:
    ops = len(latencies_ns)
    summary = _latency_summary(latencies_ns)
    return {
        "round": round_number,
        "phase": phase,
        "latencies_ns": list(latencies_ns),
        "ops": ops,
        "errors": 0,
        "elapsed_s": elapsed_s,
        "ops_per_s": ops / elapsed_s if elapsed_s > 0 else 0.0,
        "bytes": bytes_count,
        "mib_per_s": (
            bytes_count / (1024 * 1024) / elapsed_s if elapsed_s > 0 else 0.0
        ),
        **summary,
    }


def _worker_private_files(
    round_dir: Path,
    workload: WorkloadConfig,
) -> tuple[list[Path], list[Path]]:
    directories: list[Path] = []
    files: list[Path] = []
    for directory_index in range(workload.directories):
        directory = round_dir / f"d{directory_index:04d}"
        directory.mkdir()
        directories.append(directory)
    directories.append(round_dir)
    for file_index in range(workload.files_per_host):
        directory = directories[file_index % len(directories)]
        files.append(directory / f"f{file_index:06d}.bin")
    return directories, files


def _worker_shared_files(
    shared_dir: Path,
    host_name: str,
    round_number: int,
    count: int,
) -> list[Path]:
    prefix = f"{host_name}-r{round_number:04d}"
    return [shared_dir / f"{prefix}-{index:06d}.bin" for index in range(count)]


def _run_private_round(
    round_number: int,
    host_dir: Path,
    workload: WorkloadConfig,
    payload: bytes,
) -> tuple[list[dict[str, Any]], list[Path]]:
    round_dir = host_dir / f"round-{round_number:04d}"
    round_dir.mkdir()
    directories, files = _worker_private_files(round_dir, workload)
    metrics: list[dict[str, Any]] = []

    latencies: list[int] = []
    started = time.perf_counter_ns()
    for path in files:
        operation_started = time.perf_counter_ns()
        _create_file(path, payload)
        latencies.append(time.perf_counter_ns() - operation_started)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    metrics.append(
        _phase_metric(
            round_number,
            "private_create",
            elapsed_s=elapsed,
            latencies_ns=latencies,
            bytes_count=len(payload) * len(files),
        )
    )

    if workload.fsync:
        latencies = []
        started = time.perf_counter_ns()
        for path in files:
            operation_started = time.perf_counter_ns()
            _fsync_file(path)
            latencies.append(time.perf_counter_ns() - operation_started)
        for path in reversed(directories):
            operation_started = time.perf_counter_ns()
            _fsync_directory(path)
            latencies.append(time.perf_counter_ns() - operation_started)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
        metrics.append(
            _phase_metric(
                round_number,
                "private_fsync",
                elapsed_s=elapsed,
                latencies_ns=latencies,
                bytes_count=0,
            )
        )

    if workload.read_back:
        latencies = []
        bytes_read = 0
        started = time.perf_counter_ns()
        for path in files:
            operation_started = time.perf_counter_ns()
            bytes_read += _read_file(path, workload.file_size_bytes)
            latencies.append(time.perf_counter_ns() - operation_started)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
        metrics.append(
            _phase_metric(
                round_number,
                "private_read",
                elapsed_s=elapsed,
                latencies_ns=latencies,
                bytes_count=bytes_read,
            )
        )

    latencies = []
    started = time.perf_counter_ns()
    for path in files:
        operation_started = time.perf_counter_ns()
        path.stat()
        latencies.append(time.perf_counter_ns() - operation_started)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    metrics.append(
        _phase_metric(
            round_number,
            "private_stat",
            elapsed_s=elapsed,
            latencies_ns=latencies,
            bytes_count=0,
        )
    )

    renamed: list[Path] = []
    latencies = []
    started = time.perf_counter_ns()
    for path in files:
        destination = path.with_suffix(".renamed")
        operation_started = time.perf_counter_ns()
        path.rename(destination)
        latencies.append(time.perf_counter_ns() - operation_started)
        renamed.append(destination)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    metrics.append(
        _phase_metric(
            round_number,
            "private_rename",
            elapsed_s=elapsed,
            latencies_ns=latencies,
            bytes_count=0,
        )
    )
    return metrics, renamed


def _run_shared_round(
    round_number: int,
    shared_dir: Path,
    host_name: str,
    workload: WorkloadConfig,
    payload: bytes,
) -> list[dict[str, Any]]:
    files = _worker_shared_files(
        shared_dir,
        host_name,
        round_number,
        workload.shared_files_per_host,
    )
    metrics: list[dict[str, Any]] = []

    latencies: list[int] = []
    started = time.perf_counter_ns()
    for path in files:
        operation_started = time.perf_counter_ns()
        _create_file(path, payload)
        latencies.append(time.perf_counter_ns() - operation_started)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    metrics.append(
        _phase_metric(
            round_number,
            "shared_create",
            elapsed_s=elapsed,
            latencies_ns=latencies,
            bytes_count=len(payload) * len(files),
        )
    )

    if workload.fsync:
        latencies = []
        started = time.perf_counter_ns()
        for path in files:
            operation_started = time.perf_counter_ns()
            _fsync_file(path)
            latencies.append(time.perf_counter_ns() - operation_started)
        operation_started = time.perf_counter_ns()
        _fsync_directory(shared_dir)
        latencies.append(time.perf_counter_ns() - operation_started)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
        metrics.append(
            _phase_metric(
                round_number,
                "shared_fsync",
                elapsed_s=elapsed,
                latencies_ns=latencies,
                bytes_count=0,
            )
        )

    if workload.read_back:
        latencies = []
        bytes_read = 0
        started = time.perf_counter_ns()
        for path in files:
            operation_started = time.perf_counter_ns()
            bytes_read += _read_file(path, workload.file_size_bytes)
            latencies.append(time.perf_counter_ns() - operation_started)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
        metrics.append(
            _phase_metric(
                round_number,
                "shared_read",
                elapsed_s=elapsed,
                latencies_ns=latencies,
                bytes_count=bytes_read,
            )
        )

    latencies = []
    started = time.perf_counter_ns()
    for path in files:
        operation_started = time.perf_counter_ns()
        path.stat()
        latencies.append(time.perf_counter_ns() - operation_started)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    metrics.append(
        _phase_metric(
            round_number,
            "shared_stat",
            elapsed_s=elapsed,
            latencies_ns=latencies,
            bytes_count=0,
        )
    )

    latencies = []
    started = time.perf_counter_ns()
    for path in files:
        operation_started = time.perf_counter_ns()
        path.unlink()
        latencies.append(time.perf_counter_ns() - operation_started)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    metrics.append(
        _phase_metric(
            round_number,
            "shared_delete",
            elapsed_s=elapsed,
            latencies_ns=latencies,
            bytes_count=0,
        )
    )
    return metrics


@dataclass
class WorkerSession:
    host: Host
    process: subprocess.Popen[str]
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    ready_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    protocol_error: str | None = None
    exit_code: int | None = None
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None

    def stdout(self) -> str:
        return "".join(self.stdout_lines)

    def stderr(self) -> str:
        return "".join(self.stderr_lines)

    def terminate(self) -> None:
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass


def _start_worker_session(
    plan: ExperimentPlan,
    host: Host,
    deployment: dict[str, str],
) -> WorkerSession:
    remote_argv = [
        host.python,
        "-u",
        "-m",
        "agent_fs_bench.benchmarks.multi_host",
        "--worker",
        "--job",
        deployment["job_path"],
    ]
    argv = _ssh_argv(plan, host)
    argv.append(f"cd {shlex.quote(deployment['remote_dir'])} && exec " + shlex.join(remote_argv))
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise RemoteError(f"{host.name}: cannot start ssh worker: {exc}") from exc
    session = WorkerSession(host=host, process=process)

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if not line.startswith("RESULT_JSON "):
                session.stdout_lines.append(line)
            stripped = line.strip()
            if stripped == "READY":
                session.ready_event.set()
            elif stripped.startswith("RESULT_JSON "):
                raw = stripped[len("RESULT_JSON ") :]
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    session.protocol_error = f"invalid RESULT_JSON: {exc}"
                    continue
                if not isinstance(parsed, dict):
                    session.protocol_error = "RESULT_JSON is not an object"
                    continue
                session.result = parsed
        session.done_event.set()

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            session.stderr_lines.append(line)

    session.stdout_thread = threading.Thread(
        target=read_stdout, daemon=True
    )
    session.stderr_thread = threading.Thread(
        target=read_stderr, daemon=True
    )
    session.stdout_thread.start()
    session.stderr_thread.start()
    return session


def _wait_for_ready(
    sessions: Sequence[WorkerSession], timeout_s: int
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        if all(session.ready_event.is_set() for session in sessions):
            return
        failed = [
            session
            for session in sessions
            if session.done_event.is_set() and not session.ready_event.is_set()
        ]
        if failed:
            details = "; ".join(
                f"{session.host.name}: {(session.stderr().strip() or session.stdout().strip() or 'exited before READY')}"
                for session in failed
            )
            raise RemoteError(f"worker failed before start gate: {details}")
        if time.monotonic() >= deadline:
            waiting = [
                session.host.name
                for session in sessions
                if not session.ready_event.is_set()
            ]
            raise RemoteError(
                f"workers did not reach the start gate within {timeout_s}s: "
                f"{', '.join(waiting)}"
            )
        time.sleep(0.02)



def _abort_sessions(sessions: Iterable[WorkerSession]) -> None:
    for session in sessions:
        session.terminate()
    for session in sessions:
        try:
            session.process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                session.process.kill()
            except OSError:
                pass


def _ssh_argv(plan: ExperimentPlan, host: Host) -> list[str]:
    destination = f"{host.user}@{host.address}" if host.user else host.address
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o",
            f"ConnectTimeout={plan.connect_timeout_s}", "-o", "LogLevel=ERROR",
            *host.ssh_args, "-p", str(host.port), "--", destination]


def _remote_python(plan: ExperimentPlan, host: Host, source: str, data: Any) -> Any:
    completed = subprocess.run(
        [*_ssh_argv(plan, host), shlex.join([host.python, "-c", source])],
        input=json.dumps(data), text=True, capture_output=True,
        timeout=plan.command_timeout_s, check=False,
    )
    if completed.returncode:
        raise RemoteError(f"{host.name}: remote command failed ({completed.returncode}): "
                          f"{completed.stderr.strip()} {completed.stdout.strip()}")
    return json.loads(completed.stdout)


def _parallel(plan: ExperimentPlan, function: Any) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(plan.hosts)) as executor:
        pending = {executor.submit(function, host): host.name for host in plan.hosts}
        return {pending[future]: future.result() for future in as_completed(pending)}


def _preflight_host(plan: ExperimentPlan, host: Host) -> dict[str, Any]:
    return _remote_python(plan, host, """
import json, os, platform, sys
from pathlib import Path
data = json.load(sys.stdin)
target, staging = Path(data['target']), Path(data['worker_dir'])
if staging.resolve().is_relative_to(target.resolve()):
    raise RuntimeError('worker-dir resolves inside the measured target')
if sys.version_info < (3, 12) or platform.system() != 'Linux':
    raise RuntimeError('workers require Linux and Python 3.12+')
if not target.is_dir() or not os.access(target, os.W_OK | os.X_OK):
    raise RuntimeError(f'target is not an accessible writable directory: {target}')
print(json.dumps(dict(hostname=platform.node(), platform=platform.platform(),
                     machine=platform.machine(), kernel=platform.release(), python=platform.python_version(),
                     python_executable=sys.executable, uid=os.geteuid())))
""", {"target": str(plan.target), "worker_dir": str(plan.remote_root)})


def _job_for_host(plan: ExperimentPlan, host: Host) -> dict[str, Any]:
    return {
        "run_id": plan.run_id, "target": str(plan.target),
        "run_root": str(plan.run_root), "host_name": host.name,
        "host_names": [item.name for item in plan.hosts], "token": plan.token,
        "workload": asdict(plan.workload),
        "ready_timeout_s": plan.ready_timeout_s, "run_timeout_s": plan.run_timeout_s,
    }


def _deploy_host(plan: ExperimentPlan, host: Host, sources: dict[str, str]) -> dict[str, str]:
    remote_dir = plan.remote_root / host.name
    return _remote_python(plan, host, """
import json, os, sys
from pathlib import Path
data = json.load(sys.stdin)
root = Path(data['remote_dir'])
root.mkdir(parents=True, exist_ok=False, mode=0o700)
os.chmod(root, 0o700)
for relative, source in data['sources'].items():
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding='utf-8')
job = root / 'job.json'
job.write_text(json.dumps(data['job']), encoding='utf-8')
print(json.dumps(dict(remote_dir=str(root), job_path=str(job))))
""", {"remote_dir": str(remote_dir), "sources": sources, "job": _job_for_host(plan, host)})


def _initialize_run_root(plan: ExperimentPlan) -> None:
    _remote_python(plan, plan.hosts[0], """
import json, sys
from pathlib import Path
data = json.load(sys.stdin)
target, root = Path(data['target']), Path(data['run_root'])
if not root.resolve().is_relative_to(target.resolve()) or root.resolve() == target.resolve():
    raise RuntimeError('run root escapes target')
root.mkdir(parents=True, exist_ok=False)
for name in ('hosts', 'shared', '_control'):
    (root / name).mkdir()
with (root / '_control' / 'owner').open('x') as handle:
    handle.write(data['token'])
print('null')
""", {"target": str(plan.target), "run_root": str(plan.run_root), "token": plan.token})
    _parallel(plan, lambda host: _remote_python(plan, host, """
import json, sys
from pathlib import Path
data = json.load(sys.stdin)
control = Path(data['run_root']) / '_control'
if (control / 'owner').read_text() != data['token']:
    raise RuntimeError('shared filesystem owner sentinel mismatch')
with (control / ('host-' + data['host_name'])).open('x') as handle:
    handle.write(data['token'])
print('null')
""", _job_for_host(plan, host)))


def _validate_job(job: dict[str, Any]) -> tuple[Path, Path, Path]:
    target, root = Path(job["target"]), Path(job["run_root"])
    _safe_absolute(target)
    _safe_absolute(root)
    run_id, name = job["run_id"], job["host_name"]
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise WorkerError("invalid run identifier")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise WorkerError("invalid worker alias")
    if root != target / "agent-fs-bench" / "multi-host" / run_id:
        raise WorkerError("run root does not match the target and run identifier")
    if not root.resolve().is_relative_to(target.resolve()) or root.resolve() == target.resolve():
        raise WorkerError("run root escapes the target")
    host_dir, shared = root / "hosts" / name, root / "shared"
    for path in (root / "hosts", shared, root / "_control"):
        if path.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(root.resolve()):
            raise WorkerError(f"unsafe or missing run directory: {path}")
    if host_dir.exists() or host_dir.is_symlink():
        raise WorkerError(f"host directory already exists: {host_dir}")
    for sentinel in ["owner", *("host-" + alias for alias in job["host_names"])]:
        if not isinstance(sentinel, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sentinel):
            raise WorkerError("invalid sentinel alias")
        if (root / "_control" / sentinel).read_text() != job["token"]:
            raise WorkerError(f"shared filesystem sentinel mismatch: {sentinel}")
    return root, host_dir, shared


def _run_worker_job(job: dict[str, Any], paths: tuple[Path, Path, Path]) -> dict[str, Any]:
    root, host_dir, shared = paths
    workload = WorkloadConfig(**job["workload"])
    host_dir.mkdir()
    payload = _payload(job["host_name"], workload.file_size_bytes)
    started_at_ns = time.time_ns()
    metrics = []
    for round_number in range(1, workload.rounds + 1):
        print(f"[worker] round {round_number}: private", file=sys.stderr, flush=True)
        private, _ = _run_private_round(round_number, host_dir, workload, payload)
        metrics.extend(private)
        print(f"[worker] round {round_number}: shared", file=sys.stderr, flush=True)
        metrics.extend(_run_shared_round(round_number, shared, job["host_name"], workload, payload))
    finished_at_ns = time.time_ns()
    elapsed_s = (finished_at_ns - started_at_ns) / 1_000_000_000
    if workload.cleanup_private:
        shutil.rmtree(host_dir)
    return dict(success=True, host_name=job["host_name"], hostname=socket.gethostname(),
                platform=platform.platform(), machine=platform.machine(), kernel=platform.release(),
                python=platform.python_version(), pid=os.getpid(),
                started_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at_ns / 1e9)),
                host_dir=str(host_dir), shared_dir=str(shared), cleanup_private=workload.cleanup_private,
                started_at_ns=started_at_ns, finished_at_ns=finished_at_ns,
                elapsed_s=elapsed_s, run_root=str(root), metrics=metrics)


def worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Internal multi-host worker")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)

    def interrupted(_signum: int, _frame: Any) -> None:
        raise WorkerError("worker interrupted or deadline exceeded")

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(signum, interrupted)
    try:
        job = json.loads(args.job.read_text(encoding="utf-8"))
        signal.setitimer(signal.ITIMER_REAL, job["ready_timeout_s"])
        paths = _validate_job(job)
        print("READY", flush=True)
        gate_started = time.perf_counter()
        if sys.stdin.readline().strip() != "GO":
            raise WorkerError("start gate closed before GO")
        gate_wait_s = time.perf_counter() - gate_started
        signal.setitimer(signal.ITIMER_REAL, job["run_timeout_s"])
        result = _run_worker_job(job, paths)
        result["gate_wait_s"] = gate_wait_s
    except BaseException as exc:
        result = dict(success=False, error=str(exc), error_type=type(exc).__name__,
                      traceback=traceback.format_exc())
        print(result["traceback"], file=sys.stderr, flush=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print("RESULT_JSON " + json.dumps(result, separators=(",", ":")), flush=True)
    return 0 if result["success"] else 1


def _send_gate(sessions: Sequence[WorkerSession], message: str) -> float:
    started = time.perf_counter_ns()
    for session in sessions:
        if session.process.stdin is None:
            raise RemoteError(f"{session.host.name}: worker stdin unavailable")
        session.process.stdin.write(message + "\n")
        session.process.stdin.flush()
    return (time.perf_counter_ns() - started) / 1_000_000


def _drive_workers(plan: ExperimentPlan, sessions: Sequence[WorkerSession]) -> dict[str, Any]:
    _wait_for_ready(sessions, plan.ready_timeout_s)
    start_gate_ms = _send_gate(sessions, "GO")
    for session in sessions:
        if session.process.stdin:
            session.process.stdin.close()
    deadline = time.monotonic() + plan.run_timeout_s + 60
    while not all(session.done_event.is_set() for session in sessions):
        for session in sessions:
            if session.protocol_error or (session.done_event.is_set() and
                                           not (session.result or {}).get("success")):
                raise RemoteError(f"{session.host.name}: {session.protocol_error or session.result or session.stderr()}")
        if time.monotonic() >= deadline:
            raise RemoteError("workers exceeded the run deadline")
        time.sleep(0.02)
    for session in sessions:
        session.exit_code = session.process.wait(timeout=10)
        for thread in (session.stdout_thread, session.stderr_thread):
            if thread:
                thread.join(timeout=2)
        if session.exit_code or session.protocol_error or not (session.result or {}).get("success"):
            raise RemoteError(f"{session.host.name}: {session.protocol_error or session.result or session.stderr()}")
        if session.result["host_name"] != session.host.name:
            raise RemoteError(f"{session.host.name}: wrong worker identity")
    return dict(start_gate_control_write_ms=start_gate_ms)


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    _validate_config(args)
    _safe_absolute(ctx.target.path)
    worker_dir = Path(args.worker_dir)
    hosts_path = Path(args.hosts)
    _safe_absolute(worker_dir)
    if worker_dir.is_relative_to(ctx.target.path):
        raise ValueError("worker-dir must be outside the measured target")
    hosts = _load_hosts(hosts_path)
    workload = WorkloadConfig(**{key: getattr(args, key) for key in WorkloadConfig.__dataclass_fields__})
    plan = ExperimentPlan(hosts, workload, ctx.target.path, ctx.work_dir,
                          worker_dir / ctx.run_id / ctx.target.name, ctx.run_id,
                          os.urandom(24).hex(), args.connect_timeout_s, args.command_timeout_s,
                          args.ready_timeout_s, args.run_timeout_s)
    # The private hosts-file and staging paths are environment setup, not
    # experiment identity; actual hosts and worker environments are recorded.
    ctx.parameters.pop("hosts", None)
    ctx.parameters.pop("worker_dir", None)
    # SSH transport settings are environment setup, not experiment identity;
    # they remain in per-measurement details for local diagnosis.
    parameter_hosts = [
        {key: value for key, value in asdict(host).items() if key != "ssh_args"}
        for host in hosts
    ]
    ctx.parameters.update(hosts=parameter_hosts, workload=asdict(workload))
    if not shutil.which("ssh"):
        raise RemoteError("local ssh command not found")
    preflight = _parallel(plan, lambda host: _preflight_host(plan, host))
    # Worker runtime details are already retained per row; they are not part
    # of the portable experiment identity.
    sources = {str(path.relative_to(PROJECT_ROOT)): path.read_text(encoding="utf-8")
               for path in sorted((PROJECT_ROOT / "agent_fs_bench").rglob("*.py"))}
    deployments: dict[str, dict[str, str]] = {}
    sessions: list[WorkerSession] = []
    cleanup_errors: list[str] = []
    try:
        for host in hosts:
            deployments[host.name] = _deploy_host(plan, host, sources)
        _initialize_run_root(plan)
        workload_started = time.perf_counter()
        for host in hosts:
            sessions.append(_start_worker_session(plan, host, deployments[host.name]))
        gates = _drive_workers(plan, sessions)
        workload_wall_s = time.perf_counter() - workload_started
    finally:
        _abort_sessions(sessions)
        for session in sessions:
            for thread in (session.stdout_thread, session.stderr_thread):
                if thread:
                    thread.join(timeout=2)
            (ctx.result_dir / f"{session.host.name}.stdout.log").write_text(session.stdout(), encoding="utf-8")
            (ctx.result_dir / f"{session.host.name}.stderr.log").write_text(session.stderr(), encoding="utf-8")
        if not args.keep_remote:
            for host in hosts:
                if host.name not in deployments:
                    continue
                try:
                    _remote_python(plan, host, """
import json, shutil, sys
from pathlib import Path
root = Path(json.load(sys.stdin)['remote_dir'])
shutil.rmtree(root)
print('null')
""", deployments[host.name])
                except Exception as exc:
                    cleanup_errors.append(f"{host.name}: {exc}")
        if cleanup_errors:
            (ctx.result_dir / "cleanup-errors.log").write_text("\n".join(cleanup_errors) + "\n", encoding="utf-8")
            if sys.exc_info()[0] is None:
                raise RemoteError("remote cleanup failed: " + "; ".join(cleanup_errors))
    controller_environment = ctx.environment
    for session in sessions:
        result = session.result
        assert result is not None
        worker = {key: value for key, value in result.items() if key != "metrics"}
        # Each row belongs to one worker, not the controller; keep summary
        # grouping and exported hostnames truthful.
        ctx.environment = {
            **controller_environment,
            "hostname": worker["hostname"],
            "platform": worker["platform"],
            "machine": worker["machine"],
            "kernel": worker["kernel"],
            "python": worker["python"],
        }
        for measurement in result["metrics"]:
            numeric = {key: value for key, value in measurement.items()
                       if key not in ("round", "phase", "latencies_ns")}
            ctx.emit(f"{session.host.name}/{measurement['phase']}", numeric,
                     repeat=measurement["round"], details={
                         "host": asdict(session.host), "worker": worker,
                         "phase": measurement["phase"], "latencies_ns": measurement["latencies_ns"],
                         "preflight": preflight[session.host.name], "shared_sentinel_verified": True,
                         "synchronization": gates, "workload_wall_s": workload_wall_s,
                         "fsync_mode": "each file and directory in fsync phases" if workload.fsync else "disabled",
                         "cache_state": "warm: no cache drop; reads may use the host page cache",
                         "stdout": session.stdout(), "stderr": session.stderr(),
                     })
    ctx.environment = controller_environment


if __name__ == "__main__":
    if sys.argv[1:2] != ["--worker"]:
        raise SystemExit("Use python -m agent_fs_bench run multi-host; this module is only an internal worker")
    raise SystemExit(worker_main(sys.argv[2:]))
