"""Local-first CPU placement and SSH workers for mount-scale lifecycle only.

All file-operation timing belongs to the separate, global Open MPI mdtest job.
Workers keep Layout/Sampler state; JSON replies and diagnostics use local staging,
never the measured volume. A failed command leaves mounts and data for inspection.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
from queue import Empty, Queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback

from agent_fs_bench.results import PROJECT_ROOT
from agent_fs_bench.ssh import Host, ssh_argv
from .mount_scale_mdtest import _executable, _sha256
from .mount_scale_mounts import Client, Layout, load_profile
from . import mount_scale_resources as resources


@dataclass(frozen=True)
class Allocation:
    name: str
    cpus: tuple[int, ...]


def allocate(capacities: list[tuple[str, list[int]]], clients: int | None) -> tuple[Allocation, ...]:
    """Fill available CPUs in host order, without oversubscription or a sweep."""
    if not capacities or any(not cpus or len(set(cpus)) != len(cpus)
                             for _, cpus in capacities):
        raise ValueError('every host needs a non-empty, unique CPU affinity set')
    if len({name for name, _ in capacities}) != len(capacities):
        raise ValueError('host names must be unique')
    total = sum(len(cpus) for _, cpus in capacities)
    count = total if clients is None else clients
    if type(count) is not int or not 1 <= count <= total:
        raise ValueError(f'clients must be between 1 and {total} available logical CPUs')
    allocations = []
    remaining = count
    for name, cpus in capacities:
        selected = tuple(sorted(cpus)[:remaining])
        if selected:
            allocations.append(Allocation(name, selected))
            remaining -= len(selected)
        if remaining == 0:
            break
    return tuple(allocations)


def idle(seconds: float) -> dict[str, int | float]:
    before = resources.snapshot()
    time.sleep(seconds)
    after = resources.snapshot()
    return {**resources.levels(after), **resources.interval(before, after)}


def parallel(workers: list[Worker], action: str, **data) -> list[dict]:
    """Preserve host order while issuing lifecycle commands concurrently."""
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = [pool.submit(worker.call, action, **data) for worker in workers]
        return [future.result() for future in futures]


class Worker:
    """One persistent local/SSH subprocess; no restart or recovery protocol."""

    def __init__(self, name: str, host: Host | None, job: dict,
                 diagnostic_dir: Path, timeout_s: float, connect_timeout_s: int):
        self.name, self.host, self.job = name, host, job
        self.timeout_s = timeout_s
        self.package_root = PROJECT_ROOT if host is None else Path(job['deployment'])
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostic_dir = diagnostic_dir
        if host is not None:
            self._deploy(connect_timeout_s)
        python = sys.executable if host is None else host.python
        argv = [python, '-u', '-m', 'agent_fs_bench.benchmarks.mount_scale_cluster', '--worker']
        if host is not None:
            argv = [*ssh_argv(host, connect_timeout_s),
                    f'cd {shlex.quote(str(self.package_root))} && exec {shlex.join(argv)}']
        self._stderr = (diagnostic_dir / 'worker.stderr.txt').open('w')
        self.process = subprocess.Popen(
            argv, cwd=PROJECT_ROOT if host is None else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, bufsize=1,
        )
        self._replies: Queue[str | None] = Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps(job) + '\n')
            self.process.stdin.flush()
            self.info = self._reply()
        except BaseException:
            self.close(success=False)
            raise

    def _deploy(self, connect_timeout_s: int) -> None:
        sources = {str(path.relative_to(PROJECT_ROOT)): path.read_text()
                   for path in (PROJECT_ROOT / 'agent_fs_bench').rglob('*.py')}
        source = """
import json, os, sys
from pathlib import Path
job = json.load(sys.stdin)
staging = Path(job['staging'])
if not staging.is_absolute() or not staging.is_dir():
    raise ValueError(f'staging must be an existing absolute directory: {staging}')
root = Path(job['root'])
if not root.resolve().is_relative_to(staging.resolve()):
    raise ValueError('deployment escapes staging through a symlink')
root.mkdir(parents=True, exist_ok=False, mode=0o700)
os.chmod(root, 0o700)
for relative, content in job['sources'].items():
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
if job['profile'] is not None:
    profile = root / 'mounts.toml'
    profile.write_text(job['profile'])
    profile.chmod(0o600)
"""
        assert self.host is not None
        profile = Path(self.job['profiles']).read_text()
        completed = subprocess.run(
            [*ssh_argv(self.host, connect_timeout_s),
             shlex.join([self.host.python, '-c', source])],
            input=json.dumps({'root': str(self.package_root), 'sources': sources,
                              'profile': profile, 'staging': self.job['staging']}),
            text=True, capture_output=True, timeout=self.timeout_s,
        )
        (self.diagnostic_dir / 'deploy.stderr.txt').write_text(completed.stderr)
        if completed.returncode:
            raise RuntimeError(f'{self.name}: deployment failed; inspect {self.diagnostic_dir}')
        self.job['profiles'] = str(self.package_root / 'mounts.toml')

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._replies.put(line)
        self._replies.put(None)

    def _reply(self, timeout_s: float | None = None) -> dict:
        try:
            line = self._replies.get(timeout=self.timeout_s if timeout_s is None else timeout_s)
        except Empty as exc:
            raise RuntimeError(f'{self.name}: worker timed out; inspect {self.diagnostic_dir}') from exc
        if line is None:
            raise RuntimeError(f'{self.name}: worker exited; inspect {self.diagnostic_dir}')
        reply = json.loads(line)
        if 'error' in reply:
            raise RuntimeError(f"{self.name}: {reply['error']}; inspect {self.diagnostic_dir}")
        return reply['result']

    def call(self, action: str, **data) -> dict:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({'action': action, **data}) + '\n')
        self.process.stdin.flush()
        steps = self.job['directories'] if action in ('mount', 'unmount') else 1
        return self._reply(self.timeout_s * steps)

    def collect(self) -> None:
        for relative, content in self.call('diagnostics').items():
            path = self.diagnostic_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def close(self, *, success: bool) -> None:
        if self.process.poll() is None:
            if success:
                self.call('finish')
            assert self.process.stdin is not None
            self.process.stdin.close()
            if not success:
                self.process.terminate()
            self.process.wait(timeout=10)
        self._reader.join(timeout=1)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()


def preflight(job: dict) -> dict:
    if platform.system() != 'Linux' or sys.version_info < (3, 12):
        raise ValueError('mount-scale workers require Linux and Python 3.12+')
    staging = Path(job['staging'])
    if not staging.is_absolute() or not staging.is_dir():
        raise ValueError(f'staging must be an existing absolute directory: {staging}')
    if not job['directory_smoke']:
        profile = load_profile(Path(job['profiles']), job['target'])
        if (staging.resolve().is_relative_to(profile.admin_root.resolve())
                or profile.admin_root.resolve().is_relative_to(staging.resolve())):
            raise ValueError('staging must be outside admin_root')
    binary = _executable(job['mdtest'])
    version = subprocess.run([*job['mpi_launcher'], '--version'], text=True,
                             capture_output=True, check=True,
                             timeout=job['command_timeout_s']).stdout.strip()
    if 'Open MPI' not in version and 'OpenRTE' not in version:
        raise ValueError('mount-scale requires Open MPI')
    return {'hostname': platform.node(), 'cpus': sorted(os.sched_getaffinity(0)),
            'kernel': platform.release(), 'machine': platform.machine(),
            'binary': str(binary), 'mdtest_sha256': _sha256(binary),
            'mpi_version': version, 'python': sys.version.split()[0]}


class Lifecycle:
    def __init__(self, job: dict):
        self.job = job
        self.profile = (None if job['directory_smoke'] else
                        load_profile(Path(job['profiles']), job['target']))
        self.data_root = Path(job['data_root'])
        self.logs = Path(job['work_dir']) / 'diagnostics' / job['name']
        self.logs.mkdir(parents=True, exist_ok=True)
        self.layout: Layout | None = None
        self.sampler: resources.Sampler | None = None
        self.private: Path | None = None
        self.local: Path | None = None

    def command(self, request: dict) -> dict:
        action = request['action']
        if action == 'setup':
            return self.setup(request['repeat'], request['clients'], request['total_clients'])
        if action == 'mount':
            assert self.layout is not None
            result = self.layout.mount()
            return {**result, 'agent_elapsed_s': self.layout.mount_agent_elapsed_s}
        if action == 'unmount':
            assert self.layout is not None
            result = self.layout.unmount()
            return {**result, 'agent_elapsed_s': self.layout.unmount_agent_elapsed_s}
        if action == 'idle':
            return idle(self.job['idle_sample_s'])
        if action == 'sample-start':
            self.sampler = resources.Sampler(self.job['resource_sample_s'])
            self.sampler.__enter__()
            return {}
        if action == 'sample-stop':
            assert self.sampler is not None
            self.sampler.__exit__(None, None, None)
            result = self.sampler.metrics()
            self.sampler = None
            return result
        if action == 'cleanup':
            assert self.private is not None and self.local is not None
            shutil.rmtree(self.private)
            shutil.rmtree(self.local)
            self.private = self.local = None
            return {}
        if action == 'diagnostics':
            return {str(path.relative_to(self.logs)): path.read_text()
                    for path in self.logs.rglob('*.txt')}
        if action == 'finish':
            if self.sampler is not None:
                raise RuntimeError('cannot finish while resource sampling is active')
            if self.job['name'] != 'local':
                shutil.rmtree(self.job['deployment'])
            return {}
        raise ValueError(f'unknown mount-scale worker action: {action}')

    def setup(self, repeat: int, clients: int, total_clients: int) -> dict:
        if (self.data_root / '.owner').read_text() != self.job['token']:
            raise RuntimeError('admin views do not see the same run sentinel')
        name = f'N{total_clients}-R{repeat}'
        private = self.data_root / name / self.job['name']
        private.parent.mkdir(exist_ok=True)
        private.mkdir()
        local = Path(self.job['work_dir']) / name / self.job['name']
        local.mkdir(parents=True)
        shared = tuple(self.data_root / f'shared-{i}'
                       for i in range(1, self.job['directories']))
        self.private, self.local = private, local
        if self.profile is None:
            values = []
            for index in range(clients):
                workspace = private / f'agent-{index:04d}' / 'workspace'
                workspace.mkdir(parents=True)
                values.append(Client(workspace, shared))
            self.layout = None
            aliases = tuple(values)
        else:
            self.layout = Layout(self.profile, local, private, shared, clients,
                                 log_dir=self.logs / name / 'lifecycle',
                                 timeout_s=self.job['command_timeout_s'])
            aliases = self.layout.clients
        baseline = idle(self.job['idle_sample_s'])
        return {'clients': [{'workspace': str(client.workspace),
                             'shared': [str(p) for p in client.shared]} for client in aliases],
                'baseline': baseline}


def worker_main() -> int:
    job = json.loads(sys.stdin.readline())
    try:
        info = preflight(job)
        lifecycle = Lifecycle(job)
        print(json.dumps({'result': info}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            result = lifecycle.command(request)
            print(json.dumps({'result': result}), flush=True)
            if request['action'] == 'finish':
                break
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'error': str(exc)}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('internal mount-scale worker; use agent-fs-bench run mount-scale')
    raise SystemExit(worker_main())
