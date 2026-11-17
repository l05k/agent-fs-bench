"""Internal MPI ranks for the mount-scale mount and native-program workloads.

`mount` runs one site profiled mount command inside each rank: the command file holds
the site wrapper argv, the rank times only the command from invocation to its ready
return, and the wrapper's output stays in the private lifecycle work tree. `exec`
optionally joins a per-client cgroup leaf before exec'ing a native program (mdtest).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Sequence

from agent_fs_bench import cluster

MOUNT_RESULT_PREFIX = 'MOUNT_SCALE_MOUNT_RESULT '


def parse_mount_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='internal mount-scale mount rank')
    parser.add_argument('--command-file', required=True)
    parser.add_argument('--timeout', type=float)
    return parser.parse_args(argv)


def parse_mount_result(stdout: str, tasks: int) -> list[float]:
    """The synchronized per-rank wrapper durations, in rank order."""
    lines = [line for line in stdout.splitlines() if line.startswith(MOUNT_RESULT_PREFIX)]
    assert len(lines) == 1, f'mount result lines: expected 1, measured {len(lines)}'
    value = json.loads(lines[0][len(MOUNT_RESULT_PREFIX):])
    assert value['tasks'] == tasks, \
        f'mount result tasks: expected {tasks}, measured {value["tasks"]}'
    elapsed = value['rank_elapsed_s']
    assert len(elapsed) == tasks and all(
        isinstance(number, (int, float)) and math.isfinite(number) and number > 0
        for number in elapsed), \
        f'mount rank times: expected {tasks} positive finite, measured {elapsed}'
    return list(elapsed)


def private_log(path: Path):
    """Wrapper output may contain site credentials; never publish it by default."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(descriptor, 'wb')


def mount_main(argv: Sequence[str]) -> int:
    options = parse_mount_args(argv)
    cluster.report_rank_start()

    from mpi4py import MPI

    world = MPI.COMM_WORLD
    path = Path(options.command_file)
    command = json.loads(path.read_text(encoding='utf-8'))
    assert isinstance(command, list) and command and all(
        isinstance(token, str) for token in command), \
        f'{path}: expected one nonempty string argv'
    log_stdout = path.with_suffix('.stdout.txt')
    log_stderr = path.with_suffix('.stderr.txt')
    world.Barrier()
    with private_log(log_stdout) as out, private_log(log_stderr) as err:
        started = time.perf_counter()
        try:
            status = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=err, timeout=options.timeout).returncode
        except subprocess.TimeoutExpired:
            err.write(f'command exceeded {options.timeout:g}s\n'.encode())
            status = 124
        except OSError as exc:
            err.write(f'{exc}\n'.encode())
            status = 127
        elapsed = time.perf_counter() - started
    results = world.allgather((elapsed, status))
    if any(status != 0 for _, status in results):
        if world.Get_rank() == 0:
            print(f'mount command failed: statuses {[status for _, status in results]}; '
                  f'inspect {path.parent}', file=sys.stderr, flush=True)
        raise RuntimeError(f'mount command: expected status 0 on every rank, '
                           f'measured {[status for _, status in results]}')
    if world.Get_rank() == 0:
        value = {'tasks': world.Get_size(),
                 'rank_elapsed_s': [elapsed for elapsed, _ in results]}
        print(MOUNT_RESULT_PREFIX + json.dumps(value), flush=True)
    return 0


def exec_main(argv: Sequence[str]) -> int:
    """Optionally join a cgroup leaf, report startup, then replace this process."""
    cgroup = None
    if argv[:1] == ['--cgroup']:
        assert len(argv) >= 3, 'exec: expected --cgroup PATH before the program'
        cgroup = Path(argv[1])
        argv = argv[2:]
    if argv[:1] == ['--']:
        argv = argv[1:]
    assert argv, 'exec: expected a program to run'
    if cgroup is not None:
        (cgroup / 'cgroup.procs').write_text(str(os.getpid()), encoding='ascii')
    cluster.report_rank_start()
    os.execvp(argv[0], list(argv))
    return 1


def main(argv: Sequence[str]) -> int:
    if argv[:1] == ['mount']:
        return mount_main(argv[1:])
    if argv[:1] == ['exec']:
        return exec_main(argv[1:])
    raise SystemExit('internal mount-scale rank runner; use agent-fs-bench run mount-scale')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
