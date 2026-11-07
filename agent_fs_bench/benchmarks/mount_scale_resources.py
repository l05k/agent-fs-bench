"""Host-wide Linux counters, not process RSS or filesystem-attributed CPU."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time


_MEMORY_KEYS = ('MemTotal', 'MemAvailable', 'Cached', 'Dirty', 'Slab',
                'SReclaimable', 'SUnreclaim')


def _ticks(fields: list[str]) -> tuple[int, int]:
    ticks = [int(value) for value in fields]
    # guest and guest_nice are already included in user/nice: exclude duplicates.
    return sum(ticks[:3]) + sum(ticks[5:8]), sum(ticks[:8])


def snapshot(reserved: frozenset[int]) -> dict[str, int | float]:
    """Whole-host counters plus CPU time on the reserved (never rank-pinned) CPUs."""
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        name, value = line.split(':', 1)
        if name in _MEMORY_KEYS:
            memory[name] = int(value.split()[0]) * 1024
    busy = total = reserved_busy = reserved_total = 0
    for line in Path('/proc/stat').read_text().splitlines():
        name, *fields = line.split()
        if name == 'cpu':
            busy, total = _ticks(fields)
        elif name.startswith('cpu') and int(name[3:]) in reserved:
            cpu_busy, cpu_total = _ticks(fields)
            reserved_busy += cpu_busy
            reserved_total += cpu_total
    tick = os.sysconf('SC_CLK_TCK')
    return {
        'time_s': time.monotonic(),
        'cpu_busy_s': busy / tick,
        'cpu_total_s': total / tick,
        'reserved_busy_s': reserved_busy / tick,
        'reserved_total_s': reserved_total / tick,
        'used_bytes': memory['MemTotal'] - memory['MemAvailable'],
        'cached_bytes': memory['Cached'],
        'dirty_bytes': memory['Dirty'],
        'slab_bytes': memory['Slab'],
        'slab_reclaimable_bytes': memory['SReclaimable'],
        'slab_unreclaimable_bytes': memory['SUnreclaim'],
        'mount_entries': len(Path('/proc/self/mountinfo').read_text().splitlines()),
    }


def interval(before: dict, after: dict) -> dict[str, int | float]:
    """Whole-host CPU includes ranks spinning in MPI barriers; reserved CPUs do not."""
    elapsed = after['time_s'] - before['time_s']
    busy = after['cpu_busy_s'] - before['cpu_busy_s']
    total = after['cpu_total_s'] - before['cpu_total_s']
    reserved_busy = after['reserved_busy_s'] - before['reserved_busy_s']
    reserved_total = after['reserved_total_s'] - before['reserved_total_s']
    return {'sample_elapsed_s': elapsed, 'cpu_busy_s': busy,
            'cpu_busy_cores': busy / elapsed,
            'cpu_busy_percent': 100 * busy / total,
            'reserved_cpu_busy_cores': reserved_busy / elapsed,
            'reserved_cpu_busy_percent': 100 * reserved_busy / reserved_total}


def levels(sample: dict) -> dict[str, int | float]:
    return {key: value for key, value in sample.items()
            if key not in ('time_s', 'cpu_busy_s', 'cpu_total_s',
                           'reserved_busy_s', 'reserved_total_s')}


class Sampler:
    """Sample peak host memory during whole MPI jobs, including launcher overhead."""

    def __init__(self, period_s: float, reserved: frozenset[int]):
        self.period_s = period_s
        self.reserved = reserved
        self.samples: list[dict] = []
        self.stop = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        try:
            while not self.stop.wait(self.period_s):
                self.samples.append(snapshot(self.reserved))
        except Exception as error:
            self.error = error

    def __enter__(self) -> Sampler:
        self.samples.append(snapshot(self.reserved))
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join()
        self.samples.append(snapshot(self.reserved))
        if self.error is not None:
            raise self.error

    def metrics(self) -> dict[str, int | float]:
        result = interval(self.samples[0], self.samples[-1])
        result['resource_samples'] = len(self.samples)
        for key in ('used_bytes', 'cached_bytes', 'dirty_bytes', 'slab_bytes',
                    'slab_reclaimable_bytes', 'slab_unreclaimable_bytes'):
            result['peak_' + key] = max(sample[key] for sample in self.samples)
            result['peak_' + key + '_delta'] = result['peak_' + key] - self.samples[0][key]
        return result
