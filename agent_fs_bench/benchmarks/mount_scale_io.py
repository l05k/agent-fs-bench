"""Synchronized whole-file POSIX batches for mount-scale.

| Symbol | Definition |
| --- | --- |
| N | `tasks`: one MPI rank per client, each on its own mount |
| F, S | `files` and `file_size_bytes` per client |
| T | Wall time from the start barrier through the end barrier; create-write also includes each client's target-scoped syncfs |
| C | Full-file successes: a file counts only after exactly S bytes were written or read and the descriptor closed |

Every measured batch is one global Open MPI job. Each rank opens, reads or writes and
closes its own fixed file names; no file is fsynced. Create-write uses a fresh empty
directory and open(O_CREAT|O_EXCL), then all ranks check their exact counts before each
runs GNU ``sync -f -- <its directory>``. The end barrier follows those syncfs calls, so
the reported time includes data and directory-entry persistence. A row is usable only if
every rank reports exactly F full-file successes and, for create-write, every sync
succeeded. Read fixtures are prepared outside this module's measured batches.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from typing import Sequence

from agent_fs_bench import cluster
from .mount_scale_mdtest import Phase, Rank

READ = 'read'
CREATE_WRITE = 'create-write'
MODES = (READ, CREATE_WRITE)
SUBDIRECTORY = {READ: 'read', CREATE_WRITE: 'write'}
RESULT_PREFIX = 'MOUNT_SCALE_IO_RESULT '
CHUNK = 1024 * 1024
SYNC = 'sync'


def file_path(directory: Path, index: int) -> Path:
    return directory / f'file-{index:05d}.bin'


def write_all(fd: int, data: bytes) -> bool:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            return False
        view = view[written:]
    return True


def write_files(directory: Path, files: int, size: int, payload: bytes) -> tuple[int, int]:
    """Create `files` distinct S-byte files; return successful files and bytes."""
    view = memoryview(payload)
    assert len(view) == files * size
    successes = total = 0
    for index in range(files):
        fd = None
        complete = False
        try:
            fd = os.open(file_path(directory, index),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            complete = write_all(fd, view[index * size:(index + 1) * size])
        except OSError:
            complete = False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    complete = False
        if complete:
            successes += 1
            total += size
    return successes, total


def read_files(directory: Path, files: int, size: int) -> tuple[int, int]:
    """Read every byte of each file; return (files of exactly S bytes, S*count)."""
    successes = total = 0
    for index in range(files):
        fd = None
        read = 0
        try:
            fd = os.open(file_path(directory, index), os.O_RDONLY)
            while read <= size:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                read += len(chunk)
        except OSError:
            read = -1
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    read = -1
        if read == size:
            successes += 1
            total += size
    return successes, total


def syncfs(directory: Path) -> None:
    """Persist one target's data and directory entries through GNU coreutils syncfs."""
    subprocess.run([SYNC, '-f', '--', str(directory)], check=True)


def prepare_fixture(directory: Path, files: int, size: int, seed: str) -> int:
    """Create a durable read fixture through the current mount; setup time only."""
    directory.mkdir()
    successes, total = write_files(directory, files, size, random.Random(seed).randbytes(files * size))
    assert successes == files and total == files * size, \
        f'read fixture {directory}: expected {files} files of {size} bytes, ' \
        f'measured {successes} files, {total} bytes'
    syncfs(directory)
    return total


def parse_batch(label: str, stdout: str, tasks: int, mode: str, files: int,
                size: int) -> Phase:
    """Validate one job's complete-success result and turn it into a phase."""
    lines = [line for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) == 1, f'{label} result lines: expected 1, measured {len(lines)}'
    value = json.loads(lines[0][len(RESULT_PREFIX):])
    assert value['mode'] == mode and value['tasks'] == tasks, \
        f'{label} result identity: expected {mode}, {tasks} tasks, measured ' \
        f'{value["mode"]}, {value["tasks"]} tasks'
    counts = value['rank_files']
    byte_counts = value['rank_bytes']
    elapsed = value['rank_elapsed_s']
    assert counts == [files] * tasks, \
        f'{label} full-file successes: expected {files} per task, measured {counts}'
    assert byte_counts == [files * size] * tasks, \
        f'{label} full-file bytes: expected {files * size} per task, measured {byte_counts}'
    assert len(elapsed) == tasks and all(
        isinstance(number, (int, float)) and math.isfinite(number) and number > 0
        for number in elapsed), f'{label} rank times: expected {tasks} positive finite, ' \
                                f'measured {elapsed}'
    if mode == CREATE_WRITE:
        assert value['sync_ok'] is True, \
            f'{label} target syncfs: expected complete, measured {value["sync_ok"]}'
    else:
        assert value['sync_ok'] is None, \
            f'{label} read sync: expected not applicable, measured {value["sync_ok"]}'
    return Phase(mode, sum(counts), max(elapsed), list(elapsed))


@dataclass(frozen=True)
class PosixIo:
    """Run one synchronized read or create-write batch through the cluster launcher."""

    launcher: cluster.Launcher
    timeout_s: float
    startup_timeout_s: float

    def measure(self, label: str, ranks: Sequence[Rank], mode: str, files: int, size: int,
                log_dir: Path) -> Phase:
        assert ranks and mode in MODES, f'{label}: expected ranks and a known mode'
        programs = [[rank.node.python, '-u', '-m',
                     'agent_fs_bench.benchmarks.mount_scale_io',
                     '--mode', mode, '--path', str(rank.workspace / SUBDIRECTORY[mode]),
                     '--files', str(files), '--bytes', str(size)] for rank in ranks]
        for program, rank in zip(programs, ranks):
            if rank.cgroup is not None:
                program.extend(('--cgroup', str(rank.cgroup)))
        run = cluster.run_program(tuple(rank.node for rank in ranks), self.launcher, programs,
                                  log_dir, label, timeout_s=self.timeout_s,
                                  startup_timeout_s=self.startup_timeout_s)
        cluster.validate_exit(run)
        return parse_batch(label, run.stdout, len(ranks), mode, files, size)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='internal mount-scale I/O rank')
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--path', required=True)
    parser.add_argument('--files', type=int, required=True)
    parser.add_argument('--bytes', type=int, required=True)
    parser.add_argument('--cgroup', type=Path)
    args = parser.parse_args(argv)
    assert args.files >= 1 and args.bytes >= 1, \
        f'batch size: expected positive files and bytes, measured {args.files}, {args.bytes}'
    return args


def main(argv: Sequence[str]) -> int:
    options = parse_args(argv)
    if options.cgroup is not None:
        (options.cgroup / 'cgroup.procs').write_text(str(os.getpid()), encoding='ascii')
    cluster.report_rank_start()

    from mpi4py import MPI

    world = MPI.COMM_WORLD
    path = Path(options.path)
    present = path.is_dir()
    payload = (random.Random(f'mount-scale-write-{world.Get_rank()}').randbytes(options.files * options.bytes)
               if options.mode == CREATE_WRITE else b'')
    world.Barrier()
    started = time.perf_counter()
    if not present:
        successes = total = 0
    elif options.mode == READ:
        successes, total = read_files(path, options.files, options.bytes)
    else:
        successes, total = write_files(path, options.files, options.bytes, payload)
    world.Barrier()
    statuses = world.allgather((successes, total))
    complete = all(count == options.files and byte_count == options.files * options.bytes
                   for count, byte_count in statuses)
    synced = None
    if options.mode == CREATE_WRITE:
        synced = False
        if complete:
            try:
                syncfs(path)
                synced = True
            except (OSError, subprocess.CalledProcessError):
                synced = False
    world.Barrier()
    elapsed = time.perf_counter() - started
    results = world.allgather((successes, total, elapsed, synced))
    sync_ok = None if options.mode == READ else all(result[3] for result in results)
    failed = not complete or sync_ok is False
    if world.Get_rank() == 0:
        if failed:
            print(f'{options.mode} batch failed: files={statuses} syncfs={sync_ok}',
                  file=sys.stderr, flush=True)
        else:
            value = {'mode': options.mode, 'tasks': world.Get_size(),
                     'rank_files': [result[0] for result in results],
                     'rank_bytes': [result[1] for result in results],
                     'rank_elapsed_s': [result[2] for result in results],
                     'sync_ok': sync_ok}
            print(RESULT_PREFIX + json.dumps(value), flush=True)
    if failed:
        raise RuntimeError(f'{options.mode} batch: expected {options.files} full files and '
                           f'complete persistence on every task; measured {statuses}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
