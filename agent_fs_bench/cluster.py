"""Task placement and Open MPI dispatch for every workload.

``run --mode`` selects the machines a run may use:

* ``local``: only this controller.
* ``overflow``: this controller first, then the ``local/hosts.toml`` machines in file order.
* ``remote``: only the ``local/hosts.toml`` machines; the controller runs ``mpiexec`` and
  hosts no task.

Every machine keeps the same checkout (``root``, default: the controller's path) with its own
``uv sync --locked`` environment. The controller probes each machine and rejects a pool whose
hostnames repeat or whose architecture, Open MPI library or source fingerprint differ. A
workload requests ``tasks`` tasks of ``cpus_per_task`` logical CPUs with at most
``oversubscribe`` tasks per CPU group; machines fill in order, each CPU group once before any
is reused, and a request above capacity fails instead of shrinking.

One launch is one global Open MPI MPMD job with one application context per task, so
``--host`` pins each task to its machine. A wrapper checks the hostname and MPI rank, pins the
task's CPU set and reports that placement before the task runs. Python tasks (``task(t)`` in a
workload module) run under mpi4py: task 0 receives the job on stdin, barriers align their start
and end, and task 0 streams result rows to the controller on stdout. Native MPI programs
(mdtest) run through the same placement with an exec wrapper.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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

# Launcher settings of this controller and network. The keys that change measurements
# (mpiexec, mpiexec_args, env) enter the manifest only as `config.launcher_sha256`; keep
# private values out of commits.
CONFIG = {
    'hosts': PROJECT_ROOT / 'local' / 'hosts.toml',
    # Open MPI 4.1 launcher, e.g. ('/usr/bin/mpiexec.openmpi',) when another MPI is first on PATH.
    'mpiexec': ('mpiexec',),
    # Extra launcher options, e.g. ('--mca', 'btl_tcp_if_include', '10.0.0.0/24',
    # '--mca', 'oob_tcp_if_include', '10.0.0.0/24') on machines with several interfaces.
    'mpiexec_args': (),
    # Environment set in every probe and task before MPI loads.
    'env': {},
    'connect_timeout_s': 15,
    'probe_timeout_s': 120,
}

# Plain TCP plus same-host shared memory; Open MPI binding stays off because the wrapper pins.
_MCA = ('--mca', 'pml', 'ob1', '--mca', 'btl', 'self,vader,tcp',
        '--mca', 'orte_keep_fqdn_hostnames', '1',
        '--mca', 'hwloc_base_binding_policy', 'none')
# Open MPI splits application context lines on spaces and reads them into a fixed buffer.
_LINE_LIMIT = 4000
_PLACEMENT = 'AFB_PLACEMENT '
_CAPTURE_BEGIN, _CAPTURE_END = 'AFB_CAPTURE_BEGIN', 'AFB_CAPTURE_END'


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
    def cpus(self) -> tuple[int, ...]:
        return tuple(self.facts['cpus'])

    @property
    def token(self) -> str:
        """Open MPI host token."""
        return 'localhost' if self.host is None else self.host.address


@dataclass(frozen=True, eq=False)
class Pool:
    mode: str
    nodes: tuple[Node, ...]
    launcher: dict[str, str]

    def manifest(self) -> list[dict[str, Any]]:
        """Per-node facts for the run manifest: no addresses, users or SSH options."""
        return [{'name': node.name,
                 **{key: value for key, value in node.facts.items() if key != 'source_sha256'}}
                for node in self.nodes]


@dataclass(frozen=True, eq=False)
class Slot:
    """One task: its node and the logical CPUs it is pinned to."""

    node: Node
    cpus: tuple[int, ...]


@dataclass(frozen=True)
class NativeRun:
    stdout: str
    capture: str | None
    placement: list[dict[str, Any]]
    sha256: str


def tail(text: str, count: int = 20) -> str:
    return '\n'.join(text.strip().splitlines()[-count:])


def _plain(value: str, what: str) -> str:
    if not value or any(character.isspace() for character in value) or '#' in value or '//' in value:
        raise ValueError(f'{what} must be non-empty without whitespace, "#" or "//": {value!r}')
    return value


def _env_args() -> list[str]:
    return [token for key, value in CONFIG['env'].items() for token in ('--env', f'{key}={value}')]


def launcher() -> dict[str, str]:
    """Name and version of the configured launcher, which must be Open MPI 4.1."""
    command = list(CONFIG['mpiexec'])
    try:
        done = subprocess.run([*command, '--version'], capture_output=True, text=True,
                              timeout=CONFIG['probe_timeout_s'])
    except FileNotFoundError as error:
        raise ValueError(f'MPI launcher not found: {command[0]}') from error
    text = done.stdout.strip() or done.stderr.strip()
    match = re.search(r'\((?:Open MPI|OpenRTE)\)\s+(\d+)\.(\d+)\.(\d+)', text)
    if done.returncode or match is None:
        raise ValueError(f'requires Open MPI 4.1; {command[0]} reports {tail(text, 3)!r}')
    if match.group(1, 2) != ('4', '1'):
        raise ValueError(f'requires Open MPI 4.1; {command[0]} reports {".".join(match.groups())}')
    return {'name': Path(command[0]).name, 'version': '.'.join(match.groups())}


def launcher_digest() -> str:
    """SHA-256 of the launcher settings that change measurements, without their values."""
    settings = {key: list(CONFIG[key]) if key != 'env' else dict(CONFIG[key])
                for key in ('mpiexec', 'mpiexec_args', 'env')}
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def open_pool(mode: str) -> Pool:
    """Probe the machines `mode` may use, in fill order."""
    if mode not in MODES:
        raise ValueError(f'mode must be one of {", ".join(MODES)}')
    info = launcher()
    hosts = () if mode == 'local' else load_hosts(Path(CONFIG['hosts']))
    if mode != 'local' and not hosts:
        raise ValueError(f'{mode} mode needs at least one machine in {CONFIG["hosts"]}')
    if any(host.name == 'local' for host in hosts):
        raise ValueError('host name "local" is reserved for the controller')
    if len({host.address for host in hosts}) != len(hosts):
        raise ValueError('Open MPI host addresses must be unique')
    if any('@' in host.address or ':' in host.address for host in hosts):
        raise ValueError('host addresses must be hostnames or IPv4 addresses; use the user/port fields')
    entries: list[Host | None] = ([] if mode == 'remote' else [None]) + list(hosts)
    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        nodes = tuple(executor.map(_probe_node, entries))
    check_pool(nodes, info['version'], source_digest())
    return Pool(mode, nodes, info)


def check_pool(nodes: Sequence[Node], mpi_version: str, digest: str) -> None:
    """One distinct machine per node, one architecture, one source and Open MPI version."""
    hostnames = [node.hostname for node in nodes]
    if len(set(hostnames)) != len(hostnames):
        raise ValueError(f'duplicate execution hostnames {hostnames}: list each machine once')
    if len({node.facts['machine'] for node in nodes}) != 1:
        raise ValueError('all machines must share one architecture')
    for node in nodes:
        if node.facts['source_sha256'] != digest:
            raise ValueError(f'{node.name}: checkout {node.root} differs from the controller source; '
                             'sync it first')
        version = re.search(r'Open MPI v(\d+\.\d+\.\d+)', node.facts['mpi_library'])
        if version is None or version.group(1) != mpi_version:
            raise ValueError(f'{node.name}: mpi4py loads {node.facts["mpi_library"]!r}, '
                             f'but the launcher is Open MPI {mpi_version}')


def _probe_node(host: Host | None) -> Node:
    if host is None:
        name, root, python = 'local', PROJECT_ROOT, sys.executable
    else:
        name = host.name
        root = Path(host.root) if host.root else PROJECT_ROOT
        python = host.python or str(root / '.venv' / 'bin' / 'python')
    _plain(str(root), f'{name} checkout root')
    _plain(python, f'{name} Python executable')
    command = [python, '-m', 'agent_fs_bench.cluster', 'probe', *_env_args()]
    argv = command if host is None else [
        *ssh_argv(host, CONFIG['connect_timeout_s']),
        f'cd {shlex.quote(str(root))} && exec {shlex.join(command)}']
    # stdin closed: ssh would otherwise read the caller's stdin (e.g. a piped script).
    done = subprocess.run(argv, cwd=PROJECT_ROOT if host is None else None,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=CONFIG['probe_timeout_s'])
    if done.returncode:
        raise RuntimeError(f'{name}: probe failed with exit code {done.returncode}: '
                           f'{tail(done.stderr or done.stdout, 5)}')
    return Node(name, host, root, python, json.loads(done.stdout))


def capacity(node: Node, cpus_per_task: int = 1, oversubscribe: int = 1) -> int:
    return len(node.cpus) // cpus_per_task * oversubscribe


def allocate(nodes: Sequence[Node], tasks: int | None, cpus_per_task: int = 1,
             oversubscribe: int = 1) -> tuple[Slot, ...]:
    """Fill nodes in order; within a node use every CPU group once before reusing any.

    `tasks=None` fills the whole capacity. A node with fewer CPUs than `cpus_per_task`
    contributes nothing; a request above capacity is an error, never a smaller run.
    """
    for value, name in ((cpus_per_task, 'cpus_per_task'), (oversubscribe, 'oversubscribe')):
        if type(value) is not int or value < 1:
            raise ValueError(f'{name} must be a positive integer')
    total = sum(capacity(node, cpus_per_task, oversubscribe) for node in nodes)
    count = total if tasks is None else tasks
    if type(count) is not int or not 1 <= count <= total:
        raise ValueError(f'tasks must be between 1 and {total}: capacity of '
                         f'{", ".join(node.name for node in nodes)} at {cpus_per_task} CPU(s) '
                         f'per task and {oversubscribe} task(s) per CPU group')
    slots: list[Slot] = []
    for node in nodes:
        cpus = sorted(node.cpus)
        groups = [tuple(cpus[start:start + cpus_per_task])
                  for start in range(0, len(cpus) - cpus_per_task + 1, cpus_per_task)]
        for _ in range(oversubscribe):
            for group in groups:
                if len(slots) == count:
                    return tuple(slots)
                slots.append(Slot(node, group))
    return tuple(slots)


def _wrapper(slot: Slot, index: int, kind: str) -> list[str]:
    node = slot.node
    return [node.python, '-m', 'agent_fs_bench.cluster', kind, '--index', str(index),
            '--hostname', node.hostname, '--cpus', ','.join(map(str, slot.cpus)), *_env_args()]


def appfile(slots: Sequence[Slot], programs: Sequence[Sequence[str]]) -> str:
    """One application context per task, each declaring its node's whole slot budget."""
    budget = Counter(slot.node.token for slot in slots)
    lines = []
    for index, (slot, program) in enumerate(zip(slots, programs, strict=True)):
        argv = ['-np', '1', '--host', f'{slot.node.token}:{budget[slot.node.token]}',
                '--wdir', str(slot.node.root), '--bind-to', 'none', *program]
        for token in argv:
            _plain(token, f'task {index} launch argument')
        line = ' '.join(argv)
        if len(line) > _LINE_LIMIT:
            raise ValueError(f'application context for task {index} is too long for Open MPI: '
                             f'{len(line)} characters')
        lines.append(line)
    return '\n'.join(lines) + '\n'


def mpiexec(slots: Sequence[Slot], appfile_path: Path) -> list[str]:
    argv = [*CONFIG['mpiexec'], *_MCA, *CONFIG['mpiexec_args']]
    if any(slot.node.host is not None for slot in slots):
        argv += ['--mca', 'plm_rsh_agent',
                 openmpi_agent(Path(CONFIG['hosts']), CONFIG['connect_timeout_s']),
                 '--mca', 'plm_rsh_no_tree_spawn', '1']
    return [*argv, '--app', str(appfile_path)]


def check_placement(label: str, proofs: Sequence[dict[str, Any]],
                    slots: Sequence[Slot]) -> list[dict[str, Any]]:
    """Compare measured placement proofs with the plan; return the manifest record."""
    measured: dict[int, dict[str, Any]] = {}
    for proof in proofs:
        if proof['index'] in measured:
            raise RuntimeError(f'{label}: task {proof["index"]} reported its placement twice')
        measured[proof['index']] = proof
    planned = set(range(len(slots)))
    missing, extra = sorted(planned - set(measured)), sorted(set(measured) - planned)
    if missing:
        raise RuntimeError(f'{label}: no placement proof for task(s) {missing}')
    if extra:
        raise RuntimeError(f'{label}: placement proof for unknown task(s) {extra}')
    record = []
    for index, slot in enumerate(slots):
        proof = measured[index]
        if proof['hostname'] != slot.node.hostname:
            raise RuntimeError(f'{label}: task {index} executed on {proof["hostname"]}, '
                               f'planned {slot.node.hostname}')
        if list(proof['affinity']) != list(slot.cpus):
            raise RuntimeError(f'{label}: task {index} has CPU affinity {proof["affinity"]}, '
                               f'planned {list(slot.cpus)}')
        record.append({'index': index, 'node': slot.node.name, 'hostname': proof['hostname'],
                       'cpus': list(proof['affinity'])})
    return record


def run_tasks(ctx, args, *, label: str = 'tasks') -> None:
    """Run the workload's `task(t)` once per allocated task, in one Open MPI job."""
    slots = allocate(ctx.pool.nodes, args.tasks, args.cpus_per_task, args.oversubscribe)
    spec = {
        'module': f'agent_fs_bench.benchmarks.{ctx.benchmark.replace("-", "_")}',
        'benchmark': ctx.benchmark, 'run_id': ctx.run_id,
        'target': (ctx.target.name, str(ctx.target.path)), 'args': args,
        'slots': [(slot.node.name, slot.node.hostname, slot.cpus) for slot in slots],
    }
    path = ctx.result_dir / f'{label}.appfile'
    path.write_text(appfile(slots, [_wrapper(slot, index, 'rank')
                                    for index, slot in enumerate(slots)]))
    stderr_path = ctx.result_dir / f'{label}.stderr.txt'
    placed = Counter(slot.node.name for slot in slots)
    print(f'  {len(slots)} task(s): ' + ', '.join(f'{name} {count}' for name, count in placed.items()),
          flush=True)
    done = False
    with stderr_path.open('w') as stderr:
        process = subprocess.Popen(mpiexec(slots, path), cwd=PROJECT_ROOT, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(base64.b64encode(pickle.dumps(spec)).decode() + '\n')
            process.stdin.close()
            for line in process.stdout:
                kind, _, payload = line.rstrip('\n').partition(' ')
                if kind == 'AFB_ROW':
                    table, keys, metrics = json.loads(payload)
                    ctx.emit(table, keys, metrics)
                elif kind == 'AFB_INFO':
                    ctx.info.update(json.loads(payload))
                elif kind == 'AFB_NODE':
                    node, values = json.loads(payload)
                    ctx.info.setdefault('hosts', {}).setdefault(node, {}).update(values)
                elif kind == 'AFB_PLACEMENT':
                    ctx.info.setdefault('placement', {})[label] = check_placement(
                        label, json.loads(payload), slots)
                elif kind == 'AFB_LOG':
                    print(f'  {payload}', flush=True)
                elif kind == 'AFB_DONE':
                    done = True
                else:
                    print(line, end='', flush=True)
            code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
    if code or not done:
        raise RuntimeError(f'{ctx.benchmark}: task job exited with status {code}: '
                           f'{tail(stderr_path.read_text(), 10)}\ninspect {stderr_path} and '
                           'results/<bench>/<run-id>/<target>/hosts/<node>/ on each node')


def run_native(slots: Sequence[Slot], programs: Sequence[Sequence[str]], log_dir: Path,
               label: str, *, exists: Sequence[Sequence[Path]] | None = None,
               mkdirs: Sequence[Sequence[Path]] | None = None, within: Path | None = None,
               capture: Path | None = None, timeout_s: float | None = None) -> NativeRun:
    """Run one MPI program per task (e.g. mdtest) under the placement wrapper.

    On its own node, task i first requires the directories `exists[i]`, then creates
    `mkdirs[i]` (parents allowed, create-if-absent) inside the existing directory
    `within` (the measured target). Tasks of one node may create the same directory
    concurrently, so this never requires it to be empty. `capture` is a file the program
    writes on task 0's node; the wrapper returns its text through stdout, so no node
    needs a shared result directory.
    """
    count = len(slots)
    exists = exists or [()] * count
    mkdirs = mkdirs or [()] * count
    wrapped = []
    for index, (slot, program) in enumerate(zip(slots, programs, strict=True)):
        argv = _wrapper(slot, index, 'exec')
        argv += [token for path in exists[index] for token in ('--exists', str(path))]
        argv += [token for path in mkdirs[index] for token in ('--mkdir', str(path))]
        if within is not None:
            argv += ['--within', str(within)]
        if index == 0 and capture is not None:
            argv += ['--capture', str(capture)]
        wrapped.append([*argv, '--', *map(str, program)])
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f'{label}.appfile'
    path.write_text(appfile(slots, wrapped))
    stdout_path, stderr_path = log_dir / f'{label}.stdout.txt', log_dir / f'{label}.stderr.txt'
    with stdout_path.open('w') as out, stderr_path.open('w') as err:
        done = subprocess.run(mpiexec(slots, path), cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
                              stdout=out, stderr=err, text=True, timeout=timeout_s)
    stdout = stdout_path.read_text()
    if done.returncode:
        raise RuntimeError(f'{label}: MPI job failed with exit code {done.returncode}: '
                           f'{tail(stderr_path.read_text(), 5) or tail(stdout, 5)}')
    lines = stdout.splitlines()
    proofs = []
    for line in lines:
        position = line.find(_PLACEMENT)
        if position >= 0:
            try:
                proofs.append(json.loads(line[position + len(_PLACEMENT):]))
            except json.JSONDecodeError as error:
                raise RuntimeError(f'{label}: unparsable placement proof: {line}') from error
    text = None
    if capture is not None:
        try:
            begin = lines.index(_CAPTURE_BEGIN)
            end = lines.index(_CAPTURE_END, begin)
        except ValueError as error:
            raise RuntimeError(f'{label}: task 0 did not return {capture}') from error
        text = '\n'.join(lines[begin + 1:end]) + '\n'
    placement = check_placement(label, proofs, slots)
    digests = {proof['sha256'] for proof in proofs}
    if len(digests) != 1:
        raise RuntimeError(f'{label}: tasks ran different binaries: {sorted(digests)}')
    return NativeRun(stdout, text, placement, digests.pop())


class Task:
    """One task's view of a Python workload job (`task(t)` in a workload module).

    Every task runs the same `task(t)`. `leader` is the first task on each node; only task 0
    reports rows. Collective calls (`barrier`, `gather`, `batch`, `node_info`) must be made by
    every task in the same order.
    """

    def __init__(self, world, spec: dict[str, Any], index: int, protocol=None):
        node, hostname, cpus = spec['slots'][index]
        colors = {name: color for color, name in
                  enumerate(dict.fromkeys(slot[0] for slot in spec['slots']))}
        self.world = world
        self.node_comm = world.Split(colors[node], index)
        self.index, self.count = index, world.Get_size()
        self.node, self.hostname, self.cpus = node, hostname, tuple(cpus)
        self.local_index, self.local_count = self.node_comm.Get_rank(), self.node_comm.Get_size()
        self.leader = self.local_index == 0
        self.args = spec['args']
        self.benchmark, self.run_id = spec['benchmark'], spec['run_id']
        self.target = Target(spec['target'][0], Path(spec['target'][1]))
        self.run_root = self.target.path / 'agent-fs-bench' / self.benchmark / self.run_id
        self.node_dir = self.run_root / node
        self.task_dir = self.node_dir / f'task-{index:04d}'
        # Diagnostics stay in this node's checkout, at the controller's result path.
        self.log_dir = (PROJECT_ROOT / 'results' / self.benchmark / self.run_id
                        / self.target.name / 'hosts' / node)
        self._protocol = protocol

    def _send(self, kind: str, payload: str) -> None:
        if self._protocol is not None:
            self._protocol.write(f'AFB_{kind} {payload}\n')
            self._protocol.flush()

    def setup(self) -> None:
        """Create this node's work and log directories, then this task's directory."""
        if self.leader:
            target = self.target.path
            if not target.is_dir():
                raise RuntimeError(f'{self.hostname}: target directory does not exist: {target}; '
                                   'mount it first')
            if not self.run_root.resolve().is_relative_to(target.resolve()):
                raise RuntimeError(f'{self.hostname}: work directory escapes target through a '
                                   f'symlink: {self.run_root}')
            if self.log_dir.resolve().is_relative_to(target.resolve()):
                raise RuntimeError(f'{self.hostname}: results must be outside the measured target')
            self.run_root.mkdir(parents=True, exist_ok=True)
            self.node_dir.mkdir()
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
        """Start `function` on every task together.

        Returns the wall time between the start and end barriers (task 0's value is the
        batch time) and every task's result on task 0.
        """
        self.world.Barrier()
        started = time.perf_counter()
        result = function(*args)
        self.world.Barrier()
        wall = time.perf_counter() - started
        return wall, self.world.gather(result, root=0)

    def emit(self, table: str, keys: dict[str, Any], metrics: dict[str, Any]) -> None:
        """Append one result row (task 0 only); the controller prepends the target."""
        if self.index != 0:
            raise RuntimeError('only task 0 reports rows')
        self._send('ROW', json.dumps([table, keys, metrics]))

    def info(self, values: dict[str, Any]) -> None:
        """Merge run-level facts into the manifest (task 0 only)."""
        if self.index != 0:
            raise RuntimeError('only task 0 reports run facts')
        self._send('INFO', json.dumps(values, default=str))

    def node_info(self, values: dict[str, Any] | None) -> None:
        """Collective: each node leader's `values` go to the manifest under hosts/<node>."""
        entries = self.world.gather((self.node, values) if self.leader else None, root=0)
        for entry in entries or ():
            if entry is not None:
                self._send('NODE', json.dumps(entry, default=str))

    def log(self, message: str) -> None:
        """Progress line: printed by the controller for task 0, kept in stderr otherwise."""
        if self.index == 0:
            self._send('LOG', ' '.join(message.split()))
        else:
            print(message, file=sys.stderr, flush=True)


def _assignment(text: str) -> tuple[str, str]:
    key, separator, value = text.partition('=')
    if not separator or not key:
        raise argparse.ArgumentTypeError(f'expected NAME=VALUE: {text!r}')
    return key, value


def _wrapper_parser(kind: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f'agent_fs_bench.cluster {kind}')
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--hostname', required=True)
    parser.add_argument('--cpus', required=True,
                        type=lambda text: tuple(int(value) for value in text.split(',')))
    parser.add_argument('--env', action='append', default=[], type=_assignment)
    return parser


def _pin(index: int, hostname: str, cpus: tuple[int, ...]) -> dict[str, Any]:
    """Check the MPI rank and host, pin the CPU set, and return the measured placement."""
    rank = os.environ.get('OMPI_COMM_WORLD_RANK')
    if rank is None or rank.strip() != str(index):
        raise RuntimeError(f'task {index} was launched as OMPI_COMM_WORLD_RANK={rank!r}; '
                           'application contexts must follow task order')
    actual = socket.gethostname()
    if actual != hostname:
        raise RuntimeError(f'task {index} executed on {actual}, planned {hostname}')
    allowed = os.sched_getaffinity(0)
    if not set(cpus) <= allowed:
        raise RuntimeError(f'task {index} CPUs {list(cpus)} are outside the allowed affinity '
                           f'set {sorted(allowed)}')
    os.sched_setaffinity(0, cpus)
    affinity = sorted(os.sched_getaffinity(0))
    if affinity != sorted(cpus):
        raise RuntimeError(f'task {index} could not be pinned to CPUs {list(cpus)}')
    return {'index': index, 'hostname': actual, 'affinity': affinity}


def _probe_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='agent_fs_bench.cluster probe')
    parser.add_argument('--env', action='append', default=[], type=_assignment)
    options = parser.parse_args(argv)
    os.environ.update(dict(options.env))
    if platform.system() != 'Linux':
        raise RuntimeError('tasks run on Linux only')
    import mpi4py
    mpi4py.rc.initialize = mpi4py.rc.finalize = False
    from mpi4py import MPI
    print(json.dumps({
        'hostname': socket.gethostname(), 'cpus': sorted(os.sched_getaffinity(0)),
        'kernel': platform.release(), 'machine': platform.machine(),
        'python': platform.python_version(), 'mpi4py': mpi4py.__version__,
        'mpi_library': MPI.Get_library_version().replace('\x00', '').strip().splitlines()[0],
        'source_sha256': source_digest(),
    }))
    return 0


def _rank_main(argv: list[str]) -> int:
    options = _wrapper_parser('rank').parse_args(argv)
    os.environ.update(dict(options.env))
    proof = _pin(options.index, options.hostname, options.cpus)
    from mpi4py import MPI
    world = MPI.COMM_WORLD
    protocol = os.fdopen(os.dup(1), 'w', buffering=1) if world.Get_rank() == 0 else None
    # Only task 0's protocol stream reaches the controller's stdout; task and child
    # process output go to the job's stderr log.
    sys.stdout.flush()
    os.dup2(2, 1)
    try:
        if world.Get_rank() == 0:
            spec = pickle.loads(base64.b64decode(sys.stdin.buffer.readline()))
        else:
            spec = None
        spec = world.bcast(spec, root=0)
        task = Task(world, spec, options.index, protocol)
        proofs = world.gather(proof, root=0)
        task._send('PLACEMENT', json.dumps(proofs))
        task.setup()
        importlib.import_module(spec['module']).task(task)
        world.Barrier()
        task._send('DONE', '{}')
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        world.Abort(1)
    return 0


def _exec_main(argv: list[str]) -> int:
    split = argv.index('--')
    parser = _wrapper_parser('exec')
    parser.add_argument('--exists', action='append', default=[], type=Path)
    parser.add_argument('--mkdir', action='append', default=[], type=Path)
    parser.add_argument('--within', type=Path)
    parser.add_argument('--capture', type=Path)
    options = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        raise ValueError('exec needs a program after "--"')
    os.environ.update(dict(options.env))
    proof = _pin(options.index, options.hostname, options.cpus)
    for path in [*options.exists, *options.mkdir]:
        if not path.is_absolute():
            raise ValueError(f'task directories must be absolute: {path}')
    for path in options.exists:
        if not path.is_dir():
            raise RuntimeError(f'{path} is not an existing directory on {socket.gethostname()}')
    if options.within is not None:
        root = options.within
        if not root.is_dir():
            raise RuntimeError(f'target directory does not exist on {socket.gethostname()}: '
                               f'{root}; mount it first')
        for path in options.mkdir:
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'{path} escapes target {root} through a symlink')
    # Create-if-absent: sibling tasks of one machine may create the same directory.
    for path in options.mkdir:
        path.mkdir(parents=True, exist_ok=True)
    found = shutil.which(command[0])
    if found is None:
        raise RuntimeError(f'{command[0]} is not an executable on {socket.gethostname()}')
    binary = str(Path(found).resolve())
    with open(binary, 'rb') as handle:
        proof['sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
    proof['binary'] = binary
    print(_PLACEMENT + json.dumps(proof), flush=True)
    if options.capture is None:
        os.execv(binary, [binary, *command[1:]])
    code = subprocess.call([binary, *command[1:]])
    if code == 0:
        text = options.capture.read_text()
        options.capture.unlink()
        sys.stdout.write(f'{_CAPTURE_BEGIN}\n{text}{"" if text.endswith(chr(10)) else chr(10)}'
                         f'{_CAPTURE_END}\n')
        sys.stdout.flush()
    return code


def main(argv: list[str]) -> int:
    commands = {'probe': _probe_main, 'rank': _rank_main, 'exec': _exec_main}
    if not argv or argv[0] not in commands:
        raise SystemExit('internal cluster command; use agent-fs-bench run --mode ...')
    try:
        return commands[argv[0]](argv[1:])
    except Exception as error:
        traceback.print_exc()
        print(f'agent_fs_bench.cluster {argv[0]}: {error}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
