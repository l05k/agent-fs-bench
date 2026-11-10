"""Task placement and Open MPI launch: the concurrency axis of every experiment.

| Term | Definition |
| --- | --- |
| task | One independent client: one MPI rank, one process (Python `task(t)` or native mdtest) |
| N | `CONFIG['tasks']` of a workload; one value per run, curves pool runs with `--vary tasks` |
| machines | The `--mode` pool in placement order: `local` controller; `overflow` controller + hosts.toml; `remote` hosts.toml |
| placement | Machine i of M receives `N // M` tasks, plus one if `i < N % M`; ranks are contiguous per machine |
| CPU use | No binding: the kernel schedules each machine's tasks over its CPUs; N above the CPU count oversubscribes |
| launch | One Open MPI MPMD job, one appfile line per rank (`--host machine`), so the rank is the line number |
| timing source | Each workload times its own phases; Python tasks align them with MPI barriers |
| validity | `assert` on: pool (distinct hosts, one architecture, Linux, one source, Open MPI 4.1 everywhere), tasks (positive integer), placement (every rank on its planned host), binary (one SHA-256 on every machine), job (exit 0, completion message); messages read `<label>: expected <x>, measured <y>` |

No binding is deliberate: tasks are filesystem clients that block on I/O, and letting the kernel
move a runnable task to an idle CPU is how N real concurrent clients behave. Waiting ranks yield
(`mpi_yield_when_idle`) because the appfile declares exactly the tasks of each machine, so Open
MPI does not detect the oversubscription itself.

Controller ↔ task 0 protocol (Python workloads): the controller writes one base64 pickle of
`JobSpec` to the job's stdin; task 0 writes `MESSAGE_PREFIX <kind> <json>` lines to stdout:

| Kind | Payload | Meaning |
| --- | --- | --- |
| row | `[table, keys, metrics]` | One result row |
| info | `{key: value}` | Run-level facts for the target's entry in config.json |
| node | `[node, {key: value}]` | One machine's facts (`hosts/<node>`) |
| placement | `[hostname, ...]` | Measured hostname of every rank |
| log | `"text"` | Progress line, printed by the controller |
| done | `{}` | Every task finished `task(t)` |

Every other stdout line is launcher or program output.
"""

from __future__ import annotations

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import importlib
import json
import os
from pathlib import Path
import pickle
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import traceback
from typing import Any, Callable, Sequence

from .results import PROJECT_ROOT, source_digest
from .ssh import Host, load_hosts, openmpi_agent, ssh_argv
from .targets import Target

MODES = ('local', 'overflow', 'remote')
MPI_SERIES = '4.1'

# Launcher settings of this controller and network; `run` builds one `Launcher` from them and
# records its measured fields in the manifest `config.launcher`.
CONFIG = {
    'hosts_file': PROJECT_ROOT / 'local' / 'hosts.toml',
    # Open MPI 4.1 launcher, e.g. ('/usr/bin/mpiexec.openmpi',) when another MPI is first on PATH.
    'mpiexec': ('mpiexec',),
    # Extra launcher options, e.g. ('--mca', 'btl_tcp_if_include', '10.0.0.0/24',
    # '--mca', 'oob_tcp_if_include', '10.0.0.0/24') on machines with several interfaces.
    'mpiexec_args': (),
    # Environment of every probe, task and native program, e.g. {'MPI4PY_MPIABI': 'openmpi'}.
    'env': {},
    'connect_timeout_s': 15,
}

# Plain TCP plus same-host shared memory, no binding, yielding waiters (see module docstring).
MPI_OPTIONS = ('--mca', 'pml', 'ob1', '--mca', 'btl', 'self,vader,tcp',
               '--mca', 'orte_keep_fqdn_hostnames', '1', '--bind-to', 'none',
               '--mca', 'mpi_yield_when_idle', '1')

MESSAGE_PREFIX = 'AFB'
MESSAGE_KINDS = ('row', 'info', 'node', 'placement', 'log', 'done')


def encode_message(kind: str, payload: Any) -> str:
    return f'{MESSAGE_PREFIX} {kind} {json.dumps(payload, default=str)}\n'


def decode_message(line: str) -> tuple[str, Any] | None:
    prefix, _, rest = line.partition(' ')
    if prefix != MESSAGE_PREFIX:
        return None
    kind, _, payload = rest.rstrip('\n').partition(' ')
    return kind, json.loads(payload)


@dataclass(frozen=True)
class Launcher:
    """Open MPI launcher and SSH settings; the first three fields change measurements."""

    mpiexec: tuple[str, ...]
    mpiexec_args: tuple[str, ...]
    env: dict[str, str]
    hosts_file: Path
    connect_timeout_s: int

    def record(self) -> dict[str, Any]:
        return {'mpiexec': list(self.mpiexec), 'mpiexec_args': list(self.mpiexec_args),
                'env': dict(self.env)}


@dataclass(frozen=True, eq=False)
class Node:
    """One machine of the pool; `host` is None for the controller."""

    name: str
    host: Host | None
    root: Path
    python: str
    facts: dict[str, Any]

    @property
    def hostname(self) -> str:
        return self.facts['hostname']

    @property
    def mpi_host(self) -> str:
        return 'localhost' if self.host is None else self.host.address


Placement = tuple[Node, ...]  # Machine of task i at index i.


@dataclass(frozen=True, eq=False)
class Pool:
    mode: str
    nodes: tuple[Node, ...]
    launcher: Launcher
    mpi_version: str

    def manifest(self) -> list[dict[str, Any]]:
        """Per-machine facts: no addresses, users or SSH options."""
        return [{'name': node.name,
                 **{key: value for key, value in node.facts.items() if key != 'source_sha256'}}
                for node in self.nodes]


@dataclass(frozen=True)
class JobSpec:
    """What every rank of a Python workload job receives from the controller."""

    module: str
    benchmark: str
    run_id: str
    target: Target
    args: Any
    placement: tuple[tuple[str, str], ...]  # (node name, hostname) of task i.


# Machines ------------------------------------------------------------------------------------

def command_argv(host: Host | None, root: Path, launcher: Launcher, argv: Sequence[str]) -> list[str]:
    """`argv` in `root` on the controller (host None) or over SSH, with the launcher env."""
    command = ['env', *(f'{key}={value}' for key, value in launcher.env.items()), *argv]
    if host is None:
        return command
    return [*ssh_argv(host, launcher.connect_timeout_s),
            f'cd {shlex.quote(str(root))} && exec {shlex.join(command)}']


def run_on(node: Node, launcher: Launcher, argv: Sequence[str]) -> str:
    done = subprocess.run(command_argv(node.host, node.root, launcher, argv),
                          cwd=PROJECT_ROOT if node.host is None else None,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, check=True)
    return done.stdout


def probe(host: Host | None, launcher: Launcher) -> Node:
    if host is None:
        name, root, python = 'local', PROJECT_ROOT, sys.executable
    else:
        name = host.name
        root = Path(host.root) if host.root else PROJECT_ROOT
        python = host.python or str(root / '.venv' / 'bin' / 'python')
    node = Node(name, host, root, python, {})
    facts = json.loads(run_on(node, launcher, [python, '-m', 'agent_fs_bench.cluster', 'probe']))
    return Node(name, host, root, python, facts)


def launcher_version(launcher: Launcher) -> str:
    text = subprocess.run([*launcher.mpiexec, '--version'], capture_output=True,
                          text=True).stdout
    match = re.search(r'\((?:Open MPI|OpenRTE)\)\s+(\d+\.\d+\.\d+)', text)
    return match.group(1) if match else text.strip()


def open_pool(mode: str, launcher: Launcher) -> Pool:
    """Probe the machines of `mode` in placement order."""
    hosts = () if mode == 'local' else load_hosts(launcher.hosts_file)
    entries = ([] if mode == 'remote' else [None]) + list(hosts)
    with ThreadPoolExecutor(max_workers=8) as executor:
        nodes = tuple(executor.map(lambda host: probe(host, launcher), entries))
    return Pool(mode, nodes, launcher, launcher_version(launcher))


def validate_pool(pool: Pool, digest: str) -> None:
    """One distinct Linux machine per node, one architecture, one source, one Open MPI 4.1."""
    hostnames = [node.hostname for node in pool.nodes]
    assert hostnames, 'pool machines: expected >= 1, measured 0'
    assert len(set(hostnames)) == len(hostnames), \
        f'pool hostnames: expected distinct, measured {hostnames}'
    architectures = {node.facts['machine'] for node in pool.nodes}
    assert len(architectures) == 1, f'pool architectures: expected 1, measured {architectures}'
    assert pool.mpi_version.startswith(MPI_SERIES + '.'), \
        f'launcher Open MPI: expected {MPI_SERIES}.x, measured {pool.mpi_version!r}'
    for node in pool.nodes:
        facts = node.facts
        assert facts['system'] == 'Linux', \
            f'{node.name} system: expected Linux, measured {facts["system"]}'
        assert facts['source_sha256'] == digest, \
            f'{node.name} source: expected {digest}, measured {facts["source_sha256"]}'
        assert facts['mpi_library'].startswith(f'Open MPI v{pool.mpi_version},'), \
            f'{node.name} mpi4py library: expected Open MPI v{pool.mpi_version}, ' \
            f'measured {facts["mpi_library"]!r}'


# Placement -----------------------------------------------------------------------------------

def place(nodes: Sequence[Node], tasks: int) -> Placement:
    """Deal `tasks` round-robin over machines; ranks stay contiguous per machine."""
    assert type(tasks) is int and tasks >= 1, f'tasks: expected positive integer, measured {tasks!r}'
    shares = [tasks // len(nodes) + (index < tasks % len(nodes)) for index in range(len(nodes))]
    return tuple(node for node, share in zip(nodes, shares) for _ in range(share))


def machines(placement: Placement) -> list[Node]:
    """Distinct machines of a placement, in placement order."""
    return list(dict.fromkeys(placement))


def placement_record(placement: Placement) -> dict[str, int]:
    return dict(Counter(node.name for node in placement))


# Launch --------------------------------------------------------------------------------------

def appfile(placement: Placement, programs: Sequence[Sequence[str]]) -> str:
    """One application context per rank, each declaring its machine's whole task count."""
    counts = Counter(node.mpi_host for node in placement)
    return ''.join(f'-np 1 --host {node.mpi_host}:{counts[node.mpi_host]} --wdir {node.root} '
                   f'{" ".join(program)}\n' for node, program in zip(placement, programs))


def mpiexec_argv(placement: Placement, launcher: Launcher, appfile_path: Path) -> list[str]:
    argv = [*launcher.mpiexec, *MPI_OPTIONS, *launcher.mpiexec_args]
    argv += [token for key, value in launcher.env.items() for token in ('-x', f'{key}={value}')]
    if any(node.host is not None for node in placement):
        argv += ['--mca', 'plm_rsh_agent',
                 openmpi_agent(launcher.hosts_file, launcher.connect_timeout_s),
                 '--mca', 'plm_rsh_no_tree_spawn', '1']
    return [*argv, '--app', str(appfile_path)]


@dataclass(frozen=True)
class ProgramRun:
    label: str
    returncode: int
    stdout: str
    stderr_path: Path


def run_program(placement: Placement, launcher: Launcher, programs: Sequence[Sequence[str]],
                log_dir: Path, label: str, stdin: str = '') -> ProgramRun:
    """Run one MPI job (one program per rank); keep appfile, stdout and stderr in `log_dir`.

    Progress messages of task 0 are printed while the job runs.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    appfile_path, stderr_path = log_dir / f'{label}.appfile', log_dir / f'{label}.stderr.txt'
    appfile_path.write_text(appfile(placement, [list(map(str, p)) for p in programs]))
    lines = []
    with stderr_path.open('w') as stderr:
        process = subprocess.Popen(mpiexec_argv(placement, launcher, appfile_path),
                                   cwd=PROJECT_ROOT, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)
        process.stdin.write(stdin)
        process.stdin.close()
        for line in process.stdout:
            lines.append(line)
            message = decode_message(line)
            if message is not None and message[0] == 'log':
                print(f'  {message[1]}', flush=True)
        returncode = process.wait()
    stdout = ''.join(lines)
    (log_dir / f'{label}.stdout.txt').write_text(stdout)
    return ProgramRun(label, returncode, stdout, stderr_path)


def validate_exit(run: ProgramRun) -> None:
    assert run.returncode == 0, \
        f'{run.label} exit status: expected 0, measured {run.returncode} (stderr: {run.stderr_path})'


def make_directories(launcher: Launcher, directories: dict[Node, Sequence[Path]]) -> None:
    """Create each machine's directories (parents allowed, existing kept) before a native job."""
    for node, paths in directories.items():
        run_on(node, launcher, ['mkdir', '-p', *map(str, paths)])


def fetch_file(node: Node, launcher: Launcher, path: Path) -> str:
    """Return and remove a file a program wrote on `node` (e.g. rank 0's mdtest CSV)."""
    text = run_on(node, launcher, ['cat', str(path)])
    run_on(node, launcher, ['rm', str(path)])
    return text


def binary_digests(nodes: Sequence[Node], launcher: Launcher, binary: str) -> dict[str, str]:
    """SHA-256 of `binary` as each machine's PATH resolves it."""
    return {node.name: run_on(node, launcher, [node.python, '-m', 'agent_fs_bench.cluster',
                                               'digest', binary]).strip()
            for node in nodes}


def validate_binary(binary: str, digests: dict[str, str]) -> str:
    assert len(set(digests.values())) == 1, \
        f'{binary} SHA-256: expected one value on every machine, measured {digests}'
    return next(iter(digests.values()))


# Python workload jobs ------------------------------------------------------------------------

@dataclass
class JobMessages:
    rows: list[list[Any]] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    nodes: list[list[Any]] = field(default_factory=list)
    hostnames: list[str] = field(default_factory=list)
    done: bool = False


def parse_messages(stdout: str) -> JobMessages:
    messages = JobMessages()
    for line in stdout.splitlines():
        message = decode_message(line)
        if message is None:
            continue
        kind, payload = message
        if kind == 'row':
            messages.rows.append(payload)
        elif kind == 'info':
            messages.info.update(payload)
        elif kind == 'node':
            messages.nodes.append(payload)
        elif kind == 'placement':
            messages.hostnames = payload
        elif kind == 'done':
            messages.done = True
    return messages


def validate_job(run: ProgramRun, messages: JobMessages, placement: Placement) -> None:
    validate_exit(run)
    assert messages.done, f'{run.label} completion message: expected done, measured none'
    planned = [node.hostname for node in placement]
    assert messages.hostnames == planned, \
        f'{run.label} rank hostnames: expected {planned}, measured {messages.hostnames}'


def job_info(messages: JobMessages, placement: Placement) -> dict[str, Any]:
    """Run-level facts, per-machine facts (`hosts`) and task count per machine (`placement`)."""
    hosts: dict[str, dict[str, Any]] = {}
    for node, values in messages.nodes:
        hosts[node] = {**hosts.get(node, {}), **values}
    return {**messages.info, 'hosts': hosts, 'placement': placement_record(placement)}


def run_tasks(ctx, args, *, label: str = 'tasks') -> None:
    """Run the workload's `task(t)` once per task in one Open MPI job, then record its rows."""
    placement = place(ctx.pool.nodes, args.tasks)
    spec = JobSpec(f'agent_fs_bench.benchmarks.{ctx.benchmark.replace("-", "_")}',
                   ctx.benchmark, ctx.run_id, ctx.target, args,
                   tuple((node.name, node.hostname) for node in placement))
    print(f'  {len(placement)} task(s): {placement_record(placement)}', flush=True)
    program = lambda node: [node.python, '-m', 'agent_fs_bench.cluster', 'task']
    run = run_program(placement, ctx.pool.launcher, [program(node) for node in placement],
                      ctx.result_dir, label,
                      stdin=base64.b64encode(pickle.dumps(spec)).decode() + '\n')
    messages = parse_messages(run.stdout)
    validate_job(run, messages, placement)
    for table, keys, metrics in messages.rows:
        ctx.emit(table, keys, metrics)
    ctx.info.update(job_info(messages, placement))


class Task:
    """One task's view of a Python workload job (`task(t)` in a workload module).

    Every task runs the same `task(t)`. `leader` is the first task of each machine; task 0
    reports rows. Collective calls (`barrier`, `gather`, `batch`, `node_info`) must be made by
    every task in the same order.
    """

    def __init__(self, world, spec: JobSpec, protocol=None):
        self.index, self.count = world.Get_rank(), world.Get_size()
        self.node, self.hostname = spec.placement[self.index]
        colors = {name: color for color, name in
                  enumerate(dict.fromkeys(name for name, _ in spec.placement))}
        self.world = world
        self.node_comm = world.Split(colors[self.node], self.index)
        self.local_index, self.local_count = self.node_comm.Get_rank(), self.node_comm.Get_size()
        self.leader = self.local_index == 0
        self.args = spec.args
        self.benchmark, self.run_id, self.target = spec.benchmark, spec.run_id, spec.target
        self.run_root = self.target.path / 'agent-fs-bench' / self.benchmark / self.run_id
        self.node_dir = self.run_root / self.node
        self.task_dir = self.node_dir / f'task-{self.index:04d}'
        # Diagnostics stay in this machine's checkout, at the controller's result path.
        self.log_dir = (PROJECT_ROOT / 'results' / self.benchmark / self.run_id
                        / self.target.name / 'hosts' / self.node)
        self._protocol = protocol

    def _send(self, kind: str, payload: Any) -> None:
        self._protocol.write(encode_message(kind, payload))
        self._protocol.flush()

    def setup(self) -> None:
        """Create this machine's work and log directories, then this task's directory."""
        if self.leader:
            assert self.target.path.is_dir(), \
                f'{self.hostname} target directory: expected existing, measured {self.target.path}'
            self.node_dir.mkdir(parents=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.node_comm.Barrier()
        self.task_dir.mkdir()

    def barrier(self) -> None:
        self.world.Barrier()

    def node_barrier(self) -> None:
        self.node_comm.Barrier()

    def gather(self, value: Any) -> list[Any] | None:
        """Every task's value, in task order, on task 0; None elsewhere."""
        return self.world.gather(value, root=0)

    def batch(self, function: Callable[..., Any], *args: Any) -> tuple[float, list[Any] | None]:
        """Wall time between the start and end barriers around `function` on every task.

        Task 0's value is the batch time: from all tasks starting together to the slowest
        finishing. Also returns every task's result on task 0.
        """
        self.world.Barrier()
        started = time.perf_counter()
        result = function(*args)
        self.world.Barrier()
        wall = time.perf_counter() - started
        return wall, self.world.gather(result, root=0)

    def emit(self, table: str, keys: dict[str, Any], metrics: dict[str, Any]) -> None:
        """Append one result row (task 0 only); the controller prepends the target."""
        self._send('row', [table, keys, metrics])

    def info(self, values: dict[str, Any]) -> None:
        """Merge run-level facts into the manifest (task 0 only)."""
        self._send('info', values)

    def node_info(self, values: dict[str, Any] | None) -> None:
        """Collective: each machine leader's `values` go to the manifest under hosts/<node>."""
        entries = self.world.gather((self.node, values) if self.leader else None, root=0)
        for entry in entries or ():
            if entry is not None:
                self._send('node', entry)

    def log(self, message: str) -> None:
        """Progress line: printed by the controller for task 0, kept in stderr otherwise."""
        if self.index == 0:
            self._send('log', ' '.join(message.split()))
        else:
            print(message, file=sys.stderr, flush=True)


# Rank and machine entry points ---------------------------------------------------------------

def task_main() -> int:
    """One rank of a Python workload job; any exception aborts the whole job."""
    from mpi4py import MPI
    world = MPI.COMM_WORLD
    # Task 0's protocol stream reaches the controller's stdout; task and child process output
    # goes to the job's stderr log.
    protocol = os.fdopen(os.dup(1), 'w', buffering=1) if world.Get_rank() == 0 else None
    sys.stdout.flush()
    os.dup2(2, 1)
    try:
        spec = world.bcast(pickle.loads(base64.b64decode(sys.stdin.buffer.readline()))
                           if world.Get_rank() == 0 else None, root=0)
        task = Task(world, spec, protocol)
        hostnames = world.gather(socket.gethostname(), root=0)
        if task.index == 0:
            task._send('placement', hostnames)
        task.setup()
        importlib.import_module(spec.module).task(task)
        world.Barrier()
        if task.index == 0:
            task._send('done', {})
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        world.Abort(1)
    return 0


def probe_main() -> int:
    import mpi4py
    mpi4py.rc.initialize = mpi4py.rc.finalize = False
    from mpi4py import MPI
    print(json.dumps({
        'hostname': socket.gethostname(), 'cpu_model': cpu_model(),
        'cpu_count': len(os.sched_getaffinity(0)),
        'system': platform.system(), 'kernel': platform.release(),
        'machine': platform.machine(), 'python': platform.python_version(),
        'mpi4py': mpi4py.__version__,
        'mpi_library': MPI.Get_library_version().replace('\x00', '').strip().splitlines()[0],
        'source_sha256': source_digest(),
    }))
    return 0


def cpu_model() -> str:
    lscpu = subprocess.run(['lscpu'], capture_output=True, text=True, check=True).stdout
    return next(line.split(':', 1)[1].strip() for line in lscpu.splitlines()
                if line.startswith('Model name:'))


def digest_main(binary: str) -> int:
    with open(Path(shutil.which(binary)).resolve(), 'rb') as handle:
        print(hashlib.file_digest(handle, 'sha256').hexdigest())
    return 0


def main(argv: list[str]) -> int:
    if argv[0] == 'probe':
        return probe_main()
    if argv[0] == 'task':
        return task_main()
    return digest_main(argv[1])


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
