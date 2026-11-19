"""Site-owned per-mount CPU/memory budget proof for mount-scale.

The site, not this harness, creates one 1c2g cgroup per mount and places the
mount wrapper's processes in it.  These helpers only join a group, snapshot it
and validate the rank proofs; they never create, resize or remove cgroups.
Member PIDs are recorded as observed, not attributed to a FUSE daemon.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

CGROUP_ROOT = Path('/sys/fs/cgroup')
PROC_CGROUP = Path('/proc/self/cgroup')
CPU_MAX = '100000 100000'
MEMORY_MAX = 2147483648
PREFIX = 'MDBENCH_PLACEMENT '


def validate_path(path: Path, label: str) -> Path:
    """A site cgroup must be an absolute group path, not the controller root."""
    if not path.is_absolute():
        raise ValueError(f'{label}: expected an absolute path, measured {path}')
    if '..' in path.parts or path == CGROUP_ROOT or not path.is_relative_to(CGROUP_ROOT):
        raise ValueError(f'{label}: expected a path under {CGROUP_ROOT}, measured {path}')
    return path


def _check_limits(path: Path) -> None:
    cpu_max = (path / 'cpu.max').read_text(encoding='utf-8').strip()
    assert cpu_max == CPU_MAX, \
        f'{path} cpu.max: expected {CPU_MAX!r}, measured {cpu_max!r}'
    memory_text = (path / 'memory.max').read_text(encoding='utf-8').strip()
    try:
        memory_max = int(memory_text)
    except ValueError:
        raise AssertionError(
            f'{path} memory.max: expected {MEMORY_MAX}, measured {memory_text!r}'
        ) from None
    assert memory_max == MEMORY_MAX, \
        f'{path} memory.max: expected {MEMORY_MAX}, measured {memory_max}'


def _members(path: Path) -> list[int]:
    members = []
    for line in (path / 'cgroup.procs').read_text(encoding='utf-8').splitlines():
        if line:
            members.append(int(line))
    return members


def join(path: Path) -> dict:
    """Join a site-created group and return the observed placement proof."""
    path = validate_path(Path(path), 'cgroup')
    _check_limits(path)
    pid = os.getpid()
    (path / 'cgroup.procs').write_text(f'{pid}\n', encoding='utf-8')
    expected = '/' + path.relative_to(CGROUP_ROOT).as_posix()
    actual = [line[3:] for line in PROC_CGROUP.read_text(encoding='utf-8').splitlines()
              if line.startswith('0::')]
    assert actual == [expected], \
        f'{PROC_CGROUP} 0:: path: expected [{expected!r}], measured {actual!r}'
    members = _members(path)
    assert pid in members, f'{path} cgroup.procs: expected own pid {pid}, measured {members}'
    return {'cgroup': str(path), 'pid': pid, 'cpu_max': CPU_MAX,
            'memory_max': MEMORY_MAX, 'members': members}


def _counters(path: Path) -> dict[str, int]:
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line:
            continue
        key, _, value = line.partition(' ')
        try:
            values[key] = int(value)
        except ValueError:
            raise AssertionError(f'{path}: expected integer {key!r} counter, '
                                 f'measured {line!r}') from None
    return values


def _counter(values: dict[str, int], path: Path, key: str) -> int:
    assert key in values, f'{path}: expected counter {key!r}, measured {sorted(values)}'
    return values[key]


def snapshot(path: Path) -> dict:
    """Assert the fixed 1c2g limits and return the kernel counter snapshot."""
    path = Path(path)
    _check_limits(path)
    cpu = _counters(path / 'cpu.stat')
    events = _counters(path / 'memory.events')
    return {
        'cgroup': str(path),
        'cpu_nr_periods': _counter(cpu, path / 'cpu.stat', 'nr_periods'),
        'cpu_nr_throttled': _counter(cpu, path / 'cpu.stat', 'nr_throttled'),
        'cpu_throttled_usec': _counter(cpu, path / 'cpu.stat', 'throttled_usec'),
        'memory_max_events': _counter(events, path / 'memory.events', 'max'),
        'memory_oom': _counter(events, path / 'memory.events', 'oom'),
        'memory_oom_kill': _counter(events, path / 'memory.events', 'oom_kill'),
    }


def read_placements(stdout: str, paths: Sequence[Path | None]) -> list[dict]:
    """Validate one placement proof per configured rank, in rank order."""
    expected = {rank: str(path) for rank, path in enumerate(paths) if path is not None}
    proofs: dict[int, dict] = {}
    for line in stdout.splitlines():
        if not line.startswith(PREFIX):
            continue
        value = json.loads(line[len(PREFIX):])
        assert type(value) is dict, f'placement proof: expected an object, measured {value!r}'
        rank = value.get('rank')
        assert type(rank) is int, f'placement proof rank: expected an integer, measured {rank!r}'
        assert rank in expected, \
            f'placement proof rank {rank}: expected configured ranks {sorted(expected)}'
        assert rank not in proofs, f'placement proof rank {rank}: duplicate'
        cgroup = expected[rank]
        assert value.get('cgroup') == cgroup, \
            f'placement proof rank {rank} cgroup: expected {cgroup!r}, ' \
            f'measured {value.get("cgroup")!r}'
        assert value.get('cpu_max') == CPU_MAX, \
            f'placement proof rank {rank} cpu.max: expected {CPU_MAX!r}, ' \
            f'measured {value.get("cpu_max")!r}'
        memory_max = value.get('memory_max')
        assert type(memory_max) is int and memory_max == MEMORY_MAX, \
            f'placement proof rank {rank} memory.max: expected {MEMORY_MAX}, ' \
            f'measured {memory_max!r}'
        pid = value.get('pid')
        assert type(pid) is int and pid > 0, \
            f'placement proof rank {rank} pid: expected a positive integer, measured {pid!r}'
        members = value.get('members')
        assert type(members) is list and members and all(
            type(member) is int and member > 0 for member in members), \
            f'placement proof rank {rank} members: expected positive pids, measured {members!r}'
        assert pid in members, \
            f'placement proof rank {rank} members: expected own pid {pid}, measured {members}'
        proofs[rank] = value
    missing = sorted(set(expected) - set(proofs))
    assert not missing, f'placement proofs: missing configured ranks {missing}'
    return [proofs[rank] for rank in sorted(proofs)]
