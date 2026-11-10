"""Persistent per-machine lifecycle workers for mount-scale.

All file-operation timing belongs to the separate, global Open MPI mdtest jobs. Each
machine of the run keeps one worker in its own checkout (started over SSH for remote
machines) that holds Layout/Sampler state across those jobs. Protocol: the controller
writes the job, then one JSON request per line; the worker answers each with one JSON
result line. A failing worker exits with its traceback in `worker.stderr.txt`; mounts and
data stay for inspection.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
from queue import Queue
import shlex
import shutil
import subprocess
import sys
import threading
import time

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

    def __init__(self, node: cluster.Node, job: dict, diagnostic_dir: Path, timeout_s: float,
                 connect_timeout_s: int):
        self.node, self.name, self.job = node, node.name, job
        self.timeout_s = timeout_s
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostic_dir = diagnostic_dir
        argv = [node.python, '-u', '-m', 'agent_fs_bench.benchmarks.mount_scale_cluster',
                '--worker']
        if node.host is not None:
            argv = [*ssh_argv(node.host, connect_timeout_s),
                    f'cd {shlex.quote(str(node.root))} && exec {shlex.join(argv)}']
        self.process = subprocess.Popen(
            argv, cwd=PROJECT_ROOT if node.host is None else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=(diagnostic_dir / 'worker.stderr.txt').open('w'), text=True, bufsize=1,
        )
        self._replies: Queue[str | None] = Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self._send(job)
        self._reply(timeout_s)

    def _read(self) -> None:
        for line in self.process.stdout:
            self._replies.put(line)
        self._replies.put(None)

    def _send(self, value: dict) -> None:
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def _reply(self, timeout_s: float) -> dict:
        line = self._replies.get(timeout=timeout_s)
        assert line is not None, f'{self.name} worker reply: expected a result, measured exit; ' \
                                 f'inspect {self.diagnostic_dir}/worker.stderr.txt'
        return json.loads(line)

    def call(self, action: str, **data) -> dict:
        self._send({'action': action, **data})
        steps = self.job['directories'] if action in ('mount', 'unmount') else 1
        return self._reply(self.timeout_s * steps)

    def collect(self) -> None:
        for relative, content in self.call('diagnostics').items():
            path = self.diagnostic_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def close(self) -> None:
        self.call('finish')
        self.process.stdin.close()
        self.process.wait()


def preflight(job: dict) -> None:
    """Experiment preconditions on this machine, before any data is created."""
    assert platform.system() == 'Linux', f'worker system: expected Linux, measured ' \
                                         f'{platform.system()}'
    staging = Path(job['staging'])
    assert staging.is_dir(), f'staging: expected an existing directory, measured {staging}'
    if not job['directory_smoke']:
        admin = parse_profile(job['profile'], job['target']).admin_root
        assert admin.is_dir(), f'admin_root: expected mounted here, measured {admin}'
        assert not (staging.resolve().is_relative_to(admin.resolve())
                    or admin.resolve().is_relative_to(staging.resolve())), \
            f'staging: expected outside admin_root {admin}, measured {staging}'


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
        actions = {
            'init-data': self.init_data, 'setup': self.setup, 'mount': self.mount,
            'unmount': self.unmount, 'idle': self.idle, 'sample-start': self.sample_start,
            'sample-stop': self.sample_stop, 'cleanup': self.cleanup,
            'remove-repeat': self.remove_repeat, 'remove-data': self.remove_data,
            'diagnostics': self.diagnostics, 'finish': self.finish,
        }
        return actions[request['action']](request)

    def mount(self, request: dict) -> dict:
        return {**self.layout.mount(), 'agent_elapsed_s': self.layout.mount_agent_elapsed_s}

    def unmount(self, request: dict) -> dict:
        return {**self.layout.unmount(), 'agent_elapsed_s': self.layout.unmount_agent_elapsed_s}

    def idle(self, request: dict) -> dict:
        return idle(self.job['idle_sample_s'])

    def sample_start(self, request: dict) -> dict:
        self.sampler = resources.Sampler(self.job['resource_sample_s'])
        self.sampler.__enter__()
        return {}

    def sample_stop(self, request: dict) -> dict:
        self.sampler.__exit__(None, None, None)
        result, self.sampler = self.sampler.metrics(), None
        return result

    def cleanup(self, request: dict) -> dict:
        shutil.rmtree(self.private)
        shutil.rmtree(self.local)
        self.private = self.local = None
        return {}

    def remove_repeat(self, request: dict) -> dict:
        # Every machine has removed its private tree; the repeat directory must be empty.
        (self.data_root / request['name']).rmdir()
        return {}

    def remove_data(self, request: dict) -> dict:
        shutil.rmtree(self.data_root)
        return {}

    def diagnostics(self, request: dict) -> dict:
        return {str(path.relative_to(self.logs)): path.read_text()
                for path in self.logs.rglob('*.txt')}

    def finish(self, request: dict) -> dict:
        assert self.sampler is None, 'resource sampling at finish: expected stopped, measured active'
        return {}

    def init_data(self, request: dict) -> dict:
        """Create the run's data root, owner sentinel and shared resources (first machine)."""
        self.data_root.mkdir(parents=True)
        (self.data_root / '.owner').write_text(self.job['token'])
        for number in range(1, self.job['directories']):
            (self.data_root / f'shared-{number}').mkdir()
        return {}

    def setup(self, request: dict) -> dict:
        repeat, clients, total_clients = (request['repeat'], request['clients'],
                                          request['total_clients'])
        sentinel = (self.data_root / '.owner').read_text()
        assert sentinel == self.job['token'], \
            f'run sentinel in admin view: expected {self.job["token"]}, measured {sentinel}'
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
    preflight(job)
    lifecycle = Lifecycle(job)
    print(json.dumps({}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        print(json.dumps(lifecycle.command(request)), flush=True)
        if request['action'] == 'finish':
            break
    return 0


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('internal mount-scale worker; use agent-fs-bench run mount-scale')
    raise SystemExit(worker_main())
