"""Persistent per-machine lifecycle workers for mount-scale.

All file-operation timing belongs to the separate, global Open MPI mdtest jobs. Each
machine of the run keeps one worker in its own checkout (started over SSH for remote
machines) that holds Layout/Sampler state across those jobs. JSON replies and diagnostics
use local staging, never the measured volume. A failed command leaves mounts and data for
inspection.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
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

from agent_fs_bench import cluster
from agent_fs_bench.results import PROJECT_ROOT
from agent_fs_bench.ssh import ssh_argv
from .mount_scale_mounts import Client, Layout, parse_profile
from . import mount_scale_resources as resources


def idle(seconds: float) -> dict[str, int | float]:
    before = resources.snapshot()
    time.sleep(seconds)
    after = resources.snapshot()
    return {**resources.levels(after), **resources.interval(before, after)}


def parallel(workers: list[Worker], action: str, **data) -> list[dict]:
    """Preserve machine order while issuing lifecycle commands concurrently."""
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = [pool.submit(worker.call, action, **data) for worker in workers]
        return [future.result() for future in futures]


class Worker:
    """One persistent local/SSH subprocess; no restart or recovery protocol."""

    def __init__(self, node: cluster.Node, job: dict, diagnostic_dir: Path, timeout_s: float):
        self.node, self.name, self.job = node, node.name, job
        self.timeout_s = timeout_s
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostic_dir = diagnostic_dir
        argv = [node.python, '-u', '-m', 'agent_fs_bench.benchmarks.mount_scale_cluster',
                '--worker']
        if node.host is not None:
            argv = [*ssh_argv(node.host, cluster.CONFIG['connect_timeout_s']),
                    f'cd {shlex.quote(str(node.root))} && exec {shlex.join(argv)}']
        self._stderr = (diagnostic_dir / 'worker.stderr.txt').open('w')
        self.process = subprocess.Popen(
            argv, cwd=PROJECT_ROOT if node.host is None else None,
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
            self._reply()
        except BaseException:
            self.close(success=False)
            raise

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


def preflight(job: dict) -> None:
    if platform.system() != 'Linux' or sys.version_info < (3, 12):
        raise ValueError('mount-scale workers require Linux and Python 3.12+')
    staging = Path(job['staging'])
    if not staging.is_absolute() or not staging.is_dir():
        raise ValueError(f'staging must be an existing absolute directory: {staging}')
    if not Path(job['work_dir']).resolve().is_relative_to(staging.resolve()):
        raise ValueError('work directory escapes staging through a symlink')
    if not job['directory_smoke']:
        profile = parse_profile(job['profile'], job['target'])
        if not profile.admin_root.is_dir():
            raise ValueError(f'admin_root is not mounted here: {profile.admin_root}')
        if (staging.resolve().is_relative_to(profile.admin_root.resolve())
                or profile.admin_root.resolve().is_relative_to(staging.resolve())):
            raise ValueError('staging must be outside admin_root')


class Lifecycle:
    def __init__(self, job: dict):
        self.job = job
        self.profile = (None if job['directory_smoke'] else
                        parse_profile(job['profile'], job['target']))
        self.data_root = Path(job['data_root'])
        self.logs = Path(job['work_dir']) / 'diagnostics' / job['name']
        self.logs.mkdir(parents=True, exist_ok=True)
        self.layout: Layout | None = None
        self.sampler: resources.Sampler | None = None
        self.private: Path | None = None
        self.local: Path | None = None

    def command(self, request: dict) -> dict:
        action = request['action']
        if action == 'init-data':
            return self.init_data()
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
        if action == 'remove-repeat':
            # Every machine has removed its private tree; the repeat directory must be empty.
            (self.data_root / request['name']).rmdir()
            return {}
        if action == 'remove-data':
            shutil.rmtree(self.data_root)
            return {}
        if action == 'diagnostics':
            return {str(path.relative_to(self.logs)): path.read_text()
                    for path in self.logs.rglob('*.txt')}
        if action == 'finish':
            if self.sampler is not None:
                raise RuntimeError('cannot finish while resource sampling is active')
            return {}
        raise ValueError(f'unknown mount-scale worker action: {action}')

    def init_data(self) -> dict:
        """Create the run's data root, owner sentinel and shared resources (first machine)."""
        admin = self.data_root.parent if self.profile is None else self.profile.admin_root
        if not self.data_root.resolve().is_relative_to(admin.resolve()):
            raise RuntimeError('data_root escapes admin_root through a symlink')
        self.data_root.mkdir(parents=True, exist_ok=False)
        (self.data_root / '.owner').write_text(self.job['token'])
        for number in range(1, self.job['directories']):
            (self.data_root / f'shared-{number}').mkdir()
        return {}

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
        preflight(job)
        lifecycle = Lifecycle(job)
        print(json.dumps({'result': {}}), flush=True)
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
