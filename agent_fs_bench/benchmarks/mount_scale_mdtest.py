"""Measure mount scaling with the original IOR 4.0.0 mdtest binary.

Every phase is one global Open MPI 4.1 MPMD job described by an application
context file, with one app context per client rank::

    -np 1 --host <token>:<slots> <python> <rank wrapper> --index 0 ... -- <mdtest>

Each context declares its host's full slot budget, so Open MPI neither discovers
slots nor oversubscribes; ``--host`` is per app context, which is what pins a
rank to a host. Open MPI CPU binding stays disabled (``hwloc_base_binding_policy
none``) because the rank wrapper owns pinning: before exec it verifies the
actual hostname, requires its CPU in ``sched_getaffinity``, pins itself to that
single CPU, validates the paths that exist only on its own host, prints one
machine-readable placement proof, and only then ``exec``s mdtest. Placement is
therefore proved per rank instead of assumed from the requested map, and this
module never resolves or stats a remote path on the controller.

Only Open MPI 4.1 is supported (no MPICH compatibility path). The transport is
pinned to ``pml ob1`` with ``btl self,vader,tcp`` and FQDN hostnames are kept, so
an intra-cloud TCP cluster needs no RDMA/UCX assumption. The wrapper is this
module's internal CLI, executed from the deployed package root by absolute path,
so it needs no ``mpi4py`` and no working directory of its own.

mdtest runs unchanged upstream 4.0.0: one global ``MPI_COMM_WORLD``, exactly one
``-d`` path per rank (mdtest rejects several paths with ``-S`` and without
``-u``), its own phase barriers, and its own rank CSV. Every rank accesses the
same rank-independent ``mdtest.shared.<n>`` names through its own client alias.
Private phases precreate each rank's ``test-dir.0-0`` in the wrapper on the host
that owns the workspace, because mdtest creates it only for ranks below
``path_count``; each rank then creates ``mdtest_tree.<rank>.0``.

Timing comes only from mdtest's rank CSV: the ``all`` row is the aggregate
(maximum rank time including the phase-end barrier), each ``<rank>`` row is that
rank's time before the barrier. Command wall time is never a metric and rates
are never summed in Python. Only global rank 0 writes the CSV and mdtest's own
output, so the controller staging log directory needs no shared mount.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_VERSION = '4.0.0'
# Open MPI 4.1 is the only supported launcher: per-app --host slot budgets and
# the plm_rsh_agent contract are verified against that series.
_MPI_SERIES = (4, 1)
# mdtest's mdtest_test_num_t order; the rank CSV header repeats every name.
_PHASES = (
    'Directory creation', 'Directory stat', 'Directory read', 'Directory rename',
    'Directory removal', 'File creation', 'File stat', 'File read',
    'File removal', 'Tree creation', 'Tree removal',
)
_FILE_PHASES = ('File creation', 'File stat', 'File read', 'File removal')
_HEADER = ['rank', 'items'] + [f'{kind}-{name}' for name in _PHASES
                               for kind in ('rate', 'time')]
# A phase's rate starts at 2 + 2 * index, its time at 3 + 2 * index. Rank rows
# use the same layout but stop before the tree phases and omit the items column.
_TIME = {name: 3 + 2 * index for index, name in enumerate(_PHASES)}
_RANK_ROW = 2 + 2 * _PHASES.index('Tree creation')
# Open MPI may prefix or interleave its own daemon/launcher lines, so the
# upstream banner is matched by its distinctive text rather than at line start.
_BANNER = re.compile(
    r'mdtest-(\S+) was launched with (\d+) total task\(s\) on (\d+) node\(s\)'
)
# mdtest's TEST_DIR plus prep_testdir's ".<iteration>-<dir_iter>" for -i 1.
_TEST_DIR = 'test-dir.0-0'
# One-line placement proof printed by the rank wrapper before it execs mdtest.
_PLACEMENT = 'MDBENCH_PLACEMENT'
# Global Open MPI options; MCA parameters survive per-app-context re-parsing.
_MCA = (
    '--mca', 'pml', 'ob1',
    '--mca', 'btl', 'self,vader,tcp',
    '--mca', 'orte_keep_fqdn_hostnames', '1',
    '--mca', 'hwloc_base_binding_policy', 'none',
)
# Application context lines are split on spaces by Open MPI and are read into a
# fixed buffer, so no token may be quoted or long.
_LINE_LIMIT = 4000
_WRAPPER = Path('agent_fs_bench') / 'benchmarks' / 'mount_scale_mdtest.py'
_WRAPPER_ARGUMENTS = ('index', 'host', 'hostname', 'cpu', 'binary', 'workspace')

@dataclass(frozen=True)
class Phase:
    name: str
    operations: int
    elapsed_s: float
    rank_elapsed_s: list[float]

@dataclass(frozen=True)
class Rank:
    """One mdtest rank: the host it must run on and the CPU it must own."""

    host: str          # MPI host token: "localhost" for the controller.
    hostname: str      # Actual platform.node() measured on that host.
    cpu: int           # Linux sched_getaffinity CPU id to pin before exec.
    python: str        # Local sys.executable or the host's configured Python.
    package_root: Path # Deployment root containing the agent_fs_bench package.
    binary: str        # Absolute mdtest path reported by that host's preflight.
    workspace: Path    # Private test directory parent; exists on that host only.
    shared: tuple[Path, ...]

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()

def _executable(binary: str) -> Path:
    if not binary:
        raise ValueError('mdtest binary must not be empty')
    found = shutil.which(binary) if os.path.dirname(binary) == '' else binary
    if found is None:
        raise ValueError(f'mdtest executable not found on PATH: {binary}')
    path = Path(found).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f'mdtest is not an executable file: {path}')
    return path

def _options(files: int, size: int) -> tuple[str, ...]:
    for name, value, minimum in (('files', files, 1), ('size', size, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
    return ('-F', '-n', str(files), '-w', str(size), '-e', str(size), '-i', '1',
            '--warningAsErrors')

def _tail(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else ''

def _plain(value: object, what: str) -> str:
    """A token that survives an application context file unchanged."""
    if not isinstance(value, str) or not value:
        raise ValueError(f'{what} must be a non-empty string')
    if any(character.isspace() for character in value) or '#' in value or '//' in value:
        raise ValueError(f'{what} must not contain whitespace, "#" or "//": {value!r}')
    return value

def _absolute(value: object, what: str) -> str:
    text = _plain(str(value), what)
    if not Path(text).is_absolute():
        raise ValueError(f'{what} must be an absolute path: {text}')
    return text

class Mdtest:
    """Run upstream mdtest 4.0.0 file phases and report their upstream timings."""

    def __init__(self, binary: str, launcher: Sequence[str], timeout_s: float) -> None:
        if isinstance(launcher, str):
            raise ValueError('launcher must be an argv sequence, not a string')
        self._launcher = tuple(str(part) for part in launcher)
        if not self._launcher or any(not part for part in self._launcher):
            raise ValueError('launcher must be a non-empty argv')
        if ':' in self._launcher:
            raise ValueError('launcher must not contain the MPMD separator ":"')
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not timeout_s > 0.0):
            raise ValueError('timeout_s must be a positive number')
        self._timeout_s = float(timeout_s)
        self._binary = _executable(binary)
        self._sha256 = _sha256(self._binary)
        self._launcher_name, self._launcher_version = self._probe_launcher()
        self._version: str | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        """Versions and digests only: no addresses, argv or SSH credentials."""
        return {
            'mdtest': {'sha256': self._sha256, 'version': self._version},
            'launcher': {'name': self._launcher_name,
                         'version': self._launcher_version},
        }

    def prepare(self, shared_paths: Sequence[Path], files: int, size: int,
                log_dir: Path) -> None:
        """Create each shared fixture once on this controller, before measurement."""
        paths = self._local_paths(shared_paths, 'shared path')
        directory = self._log_dir(log_dir, paths)
        if not paths:
            return
        root = Path(__file__).resolve().parents[2]
        # One affinity CPU keeps the fixture job inside a single core budget and
        # exercises the same wrapper placement proof as the measured ranks.
        cpu = min(os.sched_getaffinity(0))
        for index, path in enumerate(paths, 1):
            items, _, _, _ = self._job(
                f'prepare-{index}',
                [Rank(host='localhost', hostname=platform.node(), cpu=cpu,
                      python=sys.executable, package_root=root,
                      binary=str(self._binary), workspace=path, shared=())],
                [('-S', '-C', '-d', str(path), *_options(files, size))],
                ('File creation',), directory,
            )
            if items != files:
                raise RuntimeError(f'mdtest prepared {items} items, expected {files}')

    def warm(self, ranks: Sequence[Rank], files: int, size: int, log_dir: Path) -> None:
        """Stat and read every client alias once, outside measured rows."""
        values = self._ranks(ranks)
        directory = self._log_dir(
            log_dir, [path for rank in values for path in (rank.workspace, *rank.shared)]
        )
        for resource in range(len(values[0].shared)):
            items, _, _, _ = self._job(
                f'warm-shared-{resource + 1}', values,
                [self._shared(rank.shared[resource], files, size) for rank in values],
                ('File stat', 'File read'), directory,
            )
            if items != files:
                raise RuntimeError(f'mdtest warmed {items} items, expected {files}')

    def measure(self, ranks: Sequence[Rank], files: int, size: int,
                log_dir: Path
                ) -> tuple[list[Phase], dict[str, tuple[dict[str, Any], ...]]]:
        """Return native phases and verified placements for every measured job."""
        values = self._ranks(ranks)
        directory = self._log_dir(
            log_dir, [path for rank in values for path in (rank.workspace, *rank.shared)]
        )
        items, times, per_rank, private_placement = self._job(
            'private', values,
            [self._private(rank.workspace, files, size) for rank in values],
            _FILE_PHASES, directory, private=True,
        )
        if items != files:
            raise RuntimeError(f'mdtest reported {items} items per task, expected {files}')
        placements = {'private': private_placement}
        phases = [
            Phase(f'private/{label}', len(values) * files, times[name], per_rank[name])
            for name, label in zip(_FILE_PHASES, ('create', 'stat', 'read', 'remove'))
        ]
        # One resource at a time; every client alias is one rank of the same job.
        for resource in range(len(values[0].shared)):
            items, times, per_rank, shared_placement = self._job(
                f'shared-{resource + 1}', values,
                [self._shared(rank.shared[resource], files, size) for rank in values],
                ('File stat', 'File read'), directory,
            )
            if items != files:
                raise RuntimeError(
                    f'mdtest shared-{resource + 1} reported an unexpected item count'
                )
            placements[f'shared-{resource + 1}'] = shared_placement
            phases += [
                Phase(f'shared-{resource + 1}/{label}', len(values) * files,
                      times[name], per_rank[name])
                for name, label in (('File stat', 'stat'), ('File read', 'read'))
            ]
        return phases, placements

    def _probe_launcher(self) -> tuple[str, str]:
        try:
            done = subprocess.run(
                [*self._launcher, '--version'], capture_output=True, text=True,
                timeout=self._timeout_s,
            )
        except FileNotFoundError as error:
            raise ValueError(f'MPI launcher not found: {self._launcher[0]}') from error
        if done.returncode != 0:
            raise RuntimeError(
                f'MPI launcher --version failed with exit code {done.returncode}'
            )
        text = done.stdout.strip() or done.stderr.strip()
        if not text:
            raise RuntimeError('MPI launcher reported an empty version')
        match = re.search(r'\((Open MPI|OpenRTE)\)\s+(\d+)\.(\d+)\.(\d+)\S*', text)
        if match is None:
            raise ValueError(
                'mount-scale requires Open MPI; launcher did not report an Open MPI version'
            )
        series = (int(match.group(2)), int(match.group(3)))
        if series != _MPI_SERIES:
            raise ValueError(
                f'mount-scale requires Open MPI 4.1.x, launcher reports '
                f'{match.group(2)}.{match.group(3)}.{match.group(4)}'
            )
        return Path(self._launcher[0]).name, '.'.join(match.group(2, 3, 4))

    @staticmethod
    def _shared(path: Path, files: int, size: int) -> tuple[str, ...]:
        return ('-S', '-T', '-E', '-d', str(path), *_options(files, size))

    @staticmethod
    def _private(path: Path, files: int, size: int) -> tuple[str, ...]:
        return ('-F', '-u', '-C', '-T', '-E', '-r', '-d', str(path),
                *_options(files, size))

    def _job(self, label: str, ranks: Sequence[Rank],
             groups: Sequence[Sequence[str]], phases: Sequence[str],
             log_dir: Path, *, private: bool = False
             ) -> tuple[int, dict[str, float], dict[str, list[float]],
                        tuple[dict[str, Any], ...]]:
        """Run one global MPMD job and validate its placement and timings."""
        if len(ranks) != len(groups):
            raise ValueError('every rank needs exactly one mdtest argument group')
        csv_path = log_dir / f'{label}.csv'
        # The CSV path travels inside the appfile, which Open MPI splits on spaces.
        _plain(str(csv_path), 'rank CSV path')
        appfile = log_dir / f'{label}.appfile'
        appfile.write_text(self._contexts(ranks, groups, private, csv_path))
        stdout_path = log_dir / f'{label}.stdout.txt'
        stderr_path = log_dir / f'{label}.stderr.txt'
        argv = [*self._launcher, *_MCA, '--app', str(appfile)]
        with stdout_path.open('w') as out, stderr_path.open('w') as err:
            done = subprocess.run(
                argv, stdout=out, stderr=err, text=True, timeout=self._timeout_s,
            )
        if done.returncode != 0:
            raise RuntimeError(
                f'mdtest run {label} failed with exit code {done.returncode}: '
                f'{_tail(stderr_path) or _tail(stdout_path)}'
            )
        stdout = stdout_path.read_text()
        placement = self._proved(label, stdout, ranks)
        self._version, items, times, per_rank = self._parse(
            label, stdout, csv_path, phases, len(ranks), self._nodes(ranks)
        )
        return items, times, per_rank, placement

    @staticmethod
    def _contexts(ranks: Sequence[Rank], groups: Sequence[Sequence[str]],
                  private: bool, csv_path: Path) -> str:
        """One app context per rank, each pinned to its host's full slot budget."""
        slots = Counter(rank.host for rank in ranks)
        lines = []
        for index, (rank, group) in enumerate(zip(ranks, groups)):
            wrapper = Path(_absolute(rank.package_root, 'rank package_root')) / _WRAPPER
            argv = [
                '-np', '1', '--host', f'{rank.host}:{slots[rank.host]}',
                '--wdir', _absolute(rank.package_root, 'rank package_root'),
                # Repeated per context because an application context file
                # re-parses command-line options for every line.
                '--bind-to', 'none',
                _plain(rank.python, 'rank python'), str(wrapper),
                '--index', str(index),
                '--host', rank.host,
                '--hostname', rank.hostname,
                '--cpu', str(rank.cpu),
                '--binary', _absolute(rank.binary, 'rank binary'),
                '--workspace', _absolute(rank.workspace, 'rank workspace'),
            ]
            for path in rank.shared:
                argv += ['--shared', _absolute(path, 'rank shared path')]
            if private:
                argv.append('--private')
            argv += ['--', *group, '--saveRankPerformanceDetails', str(csv_path)]
            line = ' '.join(argv)
            if len(line) > _LINE_LIMIT:
                raise ValueError(
                    f'application context for rank {index} is too long for Open MPI: '
                    f'{len(line)} characters'
                )
            lines.append(line)
        return '\n'.join(lines) + '\n'

    @staticmethod
    def _nodes(ranks: Sequence[Rank]) -> int:
        return len({rank.hostname for rank in ranks})

    @staticmethod
    def _proved(label: str, stdout: str, ranks: Sequence[Rank]
                ) -> tuple[dict[str, Any], ...]:
        """Validate the wrapper's actual placement proofs against the request."""
        proofs: dict[int, tuple[str, str, int, str]] = {}
        for line in stdout.splitlines():
            position = line.find(_PLACEMENT)
            if position < 0:
                continue
            fields: dict[str, str] = {}
            for item in line[position:].split()[1:]:
                key, separator, value = item.partition('=')
                if separator:
                    fields[key] = value
            try:
                index = int(fields['index'])
                proof = (fields['host'], fields['hostname'], int(fields['cpu']),
                         fields['affinity'])
            except (KeyError, ValueError) as error:
                raise RuntimeError(
                    f'mdtest run {label} printed an unparsable placement proof: {line.strip()}'
                ) from error
            if index in proofs:
                raise RuntimeError(f'mdtest run {label} proved rank {index} more than once')
            proofs[index] = proof
        expected = set(range(len(ranks)))
        missing, extra = sorted(expected - set(proofs)), sorted(set(proofs) - expected)
        if missing:
            raise RuntimeError(
                f'mdtest run {label} did not prove placement for rank(s) {missing}'
            )
        if extra:
            raise RuntimeError(f'mdtest run {label} proved unknown rank(s) {extra}')
        placements = []
        for index, rank in enumerate(ranks):
            host, hostname, cpu, affinity = proofs[index]
            if host != rank.host or hostname != rank.hostname:
                raise RuntimeError(
                    f'mdtest run {label} rank {index} executed on {hostname} ({host}), '
                    f'requested {rank.hostname} ({rank.host})'
                )
            if cpu != rank.cpu:
                raise RuntimeError(
                    f'mdtest run {label} rank {index} executed on CPU {cpu}, '
                    f'requested {rank.cpu}'
                )
            if affinity != str(rank.cpu):
                raise RuntimeError(
                    f'mdtest run {label} rank {index} has CPU affinity {affinity!r}, '
                    f'requested only CPU {rank.cpu}'
                )
            observed_cpu = int(affinity)
            placements.append({'index': index, 'hostname': hostname,
                               'cpu': observed_cpu, 'affinity': [observed_cpu]})
        return tuple(placements)

    @staticmethod
    def _parse(label: str, stdout: str, csv_path: Path, phases: Sequence[str],
               tasks: int, nodes: int = 1
               ) -> tuple[str, int, dict[str, float], dict[str, list[float]]]:
        match = _BANNER.search(stdout)
        if match is None:
            raise RuntimeError(f'mdtest run {label} did not print its version banner')
        version, launched, reported = (match.group(1), int(match.group(2)),
                                       int(match.group(3)))
        if version != _VERSION:
            raise RuntimeError(f'mdtest run {label} used mdtest-{version}, need {_VERSION}')
        if launched != tasks or reported != nodes:
            raise RuntimeError(
                f'mdtest run {label} launched {launched} task(s) on {reported} node(s), '
                f'expected {tasks} on {nodes}'
            )
        rows = [line.split(',') for line in csv_path.read_text().splitlines()
                if line.strip()]
        if (len(rows) < 2 or rows[0] != _HEADER or rows[1][0] != 'all'
                or len(rows[1]) != len(_HEADER)):
            raise RuntimeError(f'mdtest run {label} wrote an unexpected rank CSV')
        if len(rows) - 2 != tasks:
            raise RuntimeError(
                f'mdtest run {label} reported {tasks} tasks but wrote '
                f'{len(rows) - 2} rank rows'
            )
        try:
            items = int(rows[1][1])
            times = {name: float(rows[1][_TIME[name]])
                     for name in _PHASES if rows[1][_TIME[name]]}
        except ValueError as error:
            raise RuntimeError(f'mdtest run {label} wrote an unparsable aggregate row') from error
        per_rank: dict[str, list[float]] = {name: [] for name in _FILE_PHASES}
        for position, row in enumerate(rows[2:]):
            if row[0] != str(position) or len(row) < _RANK_ROW:
                raise RuntimeError(f'mdtest run {label} wrote an unexpected rank row')
            try:
                for name in _FILE_PHASES:
                    per_rank[name].append(float(row[_TIME[name]] or 0.0))
            except ValueError as error:
                raise RuntimeError(f'mdtest run {label} wrote an unparsable rank time') from error
        for name in phases:
            elapsed = times.get(name, 0.0)
            if (not math.isfinite(elapsed) or elapsed <= 0.0
                    or not all(math.isfinite(v) and v > 0.0 for v in per_rank[name])):
                raise RuntimeError(f'mdtest run {label} reported no time for {name}')
        return version, items, times, per_rank

    @staticmethod
    def _ranks(ranks: Sequence[Rank]) -> list[Rank]:
        """Validate the requested rank set without touching any rank's paths."""
        values = list(ranks)
        if not values:
            raise ValueError('at least one rank is required')
        hosts: dict[str, str] = {}
        hostnames: dict[str, str] = {}
        cpus: dict[tuple[str, int], int] = {}
        spaces: dict[tuple[str, str], int] = {}
        for index, rank in enumerate(values):
            if not isinstance(rank, Rank):
                raise ValueError(f'rank {index} must be a Rank')
            _plain(rank.host, f'rank {index} host')
            _plain(rank.hostname, f'rank {index} hostname')
            _plain(rank.python, f'rank {index} python')
            _absolute(rank.package_root, f'rank {index} package_root')
            _absolute(rank.binary, f'rank {index} binary')
            _absolute(rank.workspace, f'rank {index} workspace')
            for path in rank.shared:
                _absolute(path, f'rank {index} shared path')
            if isinstance(rank.cpu, bool) or not isinstance(rank.cpu, int) or rank.cpu < 0:
                raise ValueError(f'rank {index} CPU must be a non-negative integer')
            if hosts.setdefault(rank.host, rank.hostname) != rank.hostname:
                raise ValueError(
                    f'host token {rank.host} names both {hosts[rank.host]} and '
                    f'{rank.hostname}'
                )
            if hostnames.setdefault(rank.hostname, rank.host) != rank.host:
                raise ValueError(
                    f'host token {rank.host} and {hostnames[rank.hostname]} are '
                    f'aliases of the same machine {rank.hostname}'
                )
            key = (rank.hostname, rank.cpu)
            if key in cpus:
                raise ValueError(
                    f'ranks {cpus[key]} and {index} both pin CPU {rank.cpu} on '
                    f'{rank.hostname}'
                )
            cpus[key] = index
            key = (rank.host, str(rank.workspace))
            if key in spaces:
                raise ValueError(
                    f'ranks {spaces[key]} and {index} share workspace {rank.workspace}'
                )
            spaces[key] = index
        if len({len(rank.shared) for rank in values}) != 1:
            raise ValueError('every rank needs the same number of shared resources')
        if values[0].host != 'localhost':
            raise ValueError(
                'rank 0 must be the controller (host "localhost"): only global rank 0 '
                'writes the mdtest rank CSV that this process reads back'
            )
        return values

    @staticmethod
    def _local_paths(values: Sequence[Path], what: str) -> list[Path]:
        paths = []
        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute() or '@' in str(path):
                raise ValueError(f'invalid {what}: {path}')
            path = path.resolve()
            if not path.is_dir():
                raise ValueError(f'{what} is not a directory: {path}')
            paths.append(path)
        if len(set(paths)) != len(paths):
            raise ValueError(f'duplicate {what}')
        return paths

    @staticmethod
    def _log_dir(log_dir: Path, paths: Sequence[Path]) -> Path:
        directory = Path(log_dir).expanduser().resolve()
        for path in paths:
            if directory == path or directory.is_relative_to(path):
                raise ValueError(f'log directory {directory} is inside {path}')
        if directory.exists() and not directory.is_dir():
            raise ValueError(f'log directory is not a directory: {directory}')
        directory.mkdir(parents=True, exist_ok=True)
        return directory

def _wrapper_options(argv: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {'shared': [], 'private': False}
    position = 0
    while position < len(argv):
        option = argv[position]
        if option == '--':
            return values, list(argv[position + 1:])
        if option == '--private':
            values['private'] = True
            position += 1
        elif option == '--shared':
            if position + 1 >= len(argv):
                raise ValueError('--shared needs a path')
            values['shared'].append(argv[position + 1])
            position += 2
        elif option.startswith('--') and option[2:] in _WRAPPER_ARGUMENTS:
            if position + 1 >= len(argv):
                raise ValueError(f'{option} needs a value')
            values[option[2:]] = argv[position + 1]
            position += 2
        else:
            raise ValueError(f'unknown rank wrapper argument: {option}')
    raise ValueError('rank wrapper needs the mdtest command after "--"')

def _wrapper_directory(value: str, what: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise RuntimeError(
            f'{what} is not an existing absolute directory on {socket.gethostname()}: '
            f'{path}'
        )
    return path

def _wrapper(values: dict[str, Any], command: Sequence[str]) -> None:
    """Validate placement and paths, prove them, then become mdtest."""
    missing = [name for name in _WRAPPER_ARGUMENTS if name not in values]
    if missing:
        raise ValueError(f'rank wrapper needs {", ".join(missing)}')
    if not command:
        raise ValueError('rank wrapper needs the mdtest command after "--"')
    index, cpu = int(values['index']), int(values['cpu'])
    if index < 0 or cpu < 0:
        raise ValueError('rank index and CPU must be non-negative')
    declared = os.environ.get('OMPI_COMM_WORLD_RANK')
    if declared is None or declared.strip() != str(index):
        raise RuntimeError(
            f'rank {index} was launched as OMPI_COMM_WORLD_RANK={declared!r}; '
            f'app context order must match rank order'
        )
    actual = socket.gethostname()
    if actual != values['hostname']:
        raise RuntimeError(
            f'rank {index} executed on {actual}, requested {values["hostname"]}'
        )
    allowed = os.sched_getaffinity(0)
    if cpu not in allowed:
        raise RuntimeError(
            f'rank {index} CPU {cpu} is outside the allowed affinity set {sorted(allowed)}'
        )
    os.sched_setaffinity(0, {cpu})
    if os.sched_getaffinity(0) != {cpu}:
        raise RuntimeError(f'rank {index} could not be pinned to CPU {cpu}')
    workspace = _wrapper_directory(values['workspace'], 'workspace')
    for path in values['shared']:
        _wrapper_directory(path, 'shared path')
    if values['private']:
        test_dir = workspace / _TEST_DIR
        if test_dir.exists() and (not test_dir.is_dir() or any(test_dir.iterdir())):
            raise RuntimeError(f'private test directory is not empty: {test_dir}')
        test_dir.mkdir(exist_ok=True)
    binary = Path(values['binary'])
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f'mdtest is not an executable file: {binary}')
    print(f'{_PLACEMENT} index={index} host={values["host"]} hostname={actual} '
          f'cpu={cpu} affinity={",".join(map(str, sorted(os.sched_getaffinity(0))))}',
          flush=True)
    os.execv(str(binary), [str(binary), *command])

def _wrapper_main(argv: Sequence[str]) -> int:
    try:
        values, command = _wrapper_options(argv)
        _wrapper(values, command)
    except Exception as error:
        print(f'{_PLACEMENT} error: {error}', file=sys.stderr, flush=True)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(_wrapper_main(sys.argv[1:]))
