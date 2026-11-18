"""Mount-scale: synchronized native file operations on N independent mounted clients.

| Symbol | Definition |
| --- | --- |
| N | `tasks`: clients, one MPI rank each, dealt round-robin over the machines |
| F, S | `files` per client and phase (default 1000), `file_size_bytes` (default 4096) |
| T_p | Native mdtest aggregate phase time: its `all` row, the maximum task time including the end barrier |
| t_{p,r} | Rank r's mdtest phase time before the end barrier |
| X | `ops_per_s = completed operations / T`; never summed per machine |
| D_a | `controller_dispatch_elapsed_s`: controller time from notifying every worker of action a to receiving every reply |

`--workload mount` times one site wrapper command per active rank, from invocation to
ready return, on each client's real mount, and executes no I/O. `--workload
mdtest-create` runs IOR 4.0.0 mdtest's private zero-byte create (removal follows in the
job, unmeasured) on one mount per active client. The default `--workload mdtest-io` runs
native create then read with `-y` (per-file fsync) and reports only the `create-write`
and `read` phases; read follows create without a remount or cache drop. Mdtest uses one
``MPI_COMM_WORLD``, one ``-d`` per task and its own barriers and rank CSV; command wall
time is never a metric. Mounts and cleanup are outside phase timing. `--idle-mounts N`
mounts a separate run-level Layout of N idle clients on one host for the mount and
mdtest-create workloads; the cohort stays mounted across active repeats and is never
placed as ranks.

Per repeat tables: `phases`, `bandwidth` (logical mdtest file bytes per completed
phase), `ranks`, `dispatch`, `host_mounts`, `host_unmounts`, `agents`,
`resource_levels`, `resource_deltas`, `resource_workload`. An idle cohort additionally
emits run-level `idle_mounts` and `idle_unmounts` lifecycle rows.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import time
from typing import Any

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .mount_scale_cluster import Worker, parallel
from .mount_scale_mdtest import CREATE_WRITE, READ, VERSION as MDTEST_VERSION, Mdtest, Phase, Rank
from .mount_scale_mounts import Profile, parse_profile
from .mount_scale_runner import parse_mount_result

WORKLOADS = ('mount', 'mdtest-create', 'mdtest-io')
MDTEST_WORKLOADS = ('mdtest-create', 'mdtest-io')

CONFIG = {
    'profiles': PROJECT_ROOT / 'local' / 'mounts.toml',
    # One N per run, dealt round-robin over the --mode machines; N above the CPU count
    # oversubscribes them. The intended paper sweep keeps one fixed node budget and
    # measures separate N values in separate runs.
    'tasks': 64,
    'workload': 'mdtest-io',
    'idle_mounts': 0,
    'files': 1000,
    'file_size_bytes': 4096,
    'repeats': 3,
    'mdtest': 'mdtest',  # Used only by mdtest-create and mdtest-io.
    'command_timeout_s': 360,  # Each mount/unmount command and short worker replies.
    'lifecycle_timeout_s': 900,  # One worker's complete mount or unmount action.
    'startup_timeout_s': 180,  # Before all ranks report they started, within job_timeout_s.
    'job_timeout_s': 900,  # Each native MPI job: mount or mdtest.
    'fixture_timeout_s': 1800,  # Cleanup control replies.
    'idle_sample_s': 1.0,
    'resource_sample_s': 0.1,
}

FIGURES = [
    Figure('phases', 'tasks', 'ops_per_s', split=('topology', 'phase'),
           xlabel='Tasks', ylabel='Throughput (Kfiles/s)', yscale=1000,
           logx=True, name='throughput'),
    Figure('dispatch', 'tasks', 'controller_dispatch_elapsed_s', split=('topology', 'action'),
           xlabel='Tasks', ylabel='Controller dispatch time (s)',
           logx=True, name='mount-dispatch'),
    Figure('resource_deltas', 'tasks', 'used_bytes_delta', where={'state': 'mounted-idle'},
           split=('topology',), xlabel='Tasks', ylabel='Memory delta (MiB)',
           yscale=1024 * 1024, logx=True, name='memory-delta'),
]

MIB = 1024 * 1024
LEVELS = ('used_bytes', 'cached_bytes', 'dirty_bytes', 'slab_bytes',
          'slab_reclaimable_bytes', 'slab_unreclaimable_bytes', 'mount_entries')
INTERVAL = ('sample_elapsed_s', 'cpu_busy_s', 'cpu_busy_cores', 'cpu_busy_percent')
DELTAS = ('used_bytes', 'slab_bytes', 'slab_unreclaimable_bytes', 'mount_entries')
PEAKS = ('used_bytes', 'cached_bytes', 'dirty_bytes', 'slab_bytes',
         'slab_reclaimable_bytes', 'slab_unreclaimable_bytes')
DATA_PHASES = (READ, CREATE_WRITE)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload', choices=WORKLOADS, default=CONFIG['workload'],
                        help='mount times one site mount wrapper per active rank; '
                             'mdtest-create runs a native zero-byte private create; '
                             'mdtest-io runs native create then read with per-file fsync '
                             'in create.')
    parser.add_argument('--idle-mounts', type=int, default=CONFIG['idle_mounts'],
                        help='Run-level idle client mounts on one host, mount or mdtest-create '
                             'only; they stay mounted across active repeats and are never ranks.')
    for name, kind in (('tasks', int), ('repeats', int), ('startup_timeout_s', float),
                       ('job_timeout_s', float), ('fixture_timeout_s', float)):
        parser.add_argument('--' + name.replace('_', '-'), type=kind, default=CONFIG[name])
    options = parser.parse_args(argv)
    values = {**CONFIG, **vars(options)}
    args = argparse.Namespace(**values)
    for name in ('tasks', 'files', 'file_size_bytes', 'repeats',
                 'command_timeout_s', 'lifecycle_timeout_s', 'startup_timeout_s',
                 'job_timeout_s', 'fixture_timeout_s', 'idle_sample_s', 'resource_sample_s'):
        value = getattr(args, name)
        assert type(value) in (int, float) and math.isfinite(value) and value > 0, \
            f'{name}: expected positive finite number, measured {value!r}'
    assert type(args.idle_mounts) is int and args.idle_mounts >= 0, \
        f'idle_mounts: expected a nonnegative integer, measured {args.idle_mounts!r}'
    if args.idle_mounts:
        assert args.workload in ('mount', 'mdtest-create'), \
            f'--idle-mounts: expected workload mount or mdtest-create, measured {args.workload}'
    assert args.startup_timeout_s < args.job_timeout_s, \
        'startup_timeout_s must be shorter than job_timeout_s'
    return args


# Metrics (pure) ------------------------------------------------------------------------------

def phase_metrics(phase: Phase, tasks: int) -> dict[str, float]:
    rate = phase.operations / phase.elapsed_s
    return {'ops': phase.operations, 'elapsed_s': phase.elapsed_s, 'ops_per_s': rate,
            'ops_per_s_per_task': rate / tasks,
            'rank_mean_us_per_op': sum(phase.rank_elapsed_s) / phase.operations * 1e6}


def bandwidth_metrics(phase: Phase, file_size_bytes: int) -> dict[str, float]:
    """Logical completed file bytes; read bytes are not physical network/disk bytes."""
    byte_count = phase.operations * file_size_bytes
    return {'bytes': byte_count, 'elapsed_s': phase.elapsed_s,
            'mib_per_s': byte_count / phase.elapsed_s / MIB}


def level_metrics(sample: dict) -> dict[str, float]:
    return {key: sample[key] for key in (*LEVELS, *INTERVAL)}


def delta_metrics(sample: dict, baseline: dict) -> dict[str, float]:
    return {key + '_delta': sample[key] - baseline[key] for key in DELTAS}


def workload_metrics(sample: dict) -> dict[str, float]:
    return {**{key: sample[key] for key in INTERVAL},
            **{f'peak_{key}': sample[f'peak_{key}'] for key in PEAKS},
            **{f'peak_{key}_delta': sample[f'peak_{key}_delta'] for key in PEAKS}}


# Experiment state ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Experiment:
    topology: str
    profile: Profile
    data_root: Path
    placement: cluster.Placement
    workers: list[Worker]
    engine: Mdtest | None
    launcher: cluster.Launcher


@dataclass(frozen=True)
class Repeat:
    """Raw samples of one repeat."""

    number: int
    name: str
    baselines: list[dict]
    mounted: list[dict]
    workload: list[dict | None]
    released: list[dict]
    mounts: list[dict]
    unmounts: list[dict]
    dispatch_s: dict[str, float]
    phases: list[Phase]
    hosts: list[str]


def load_profile(ctx: RunContext, args: argparse.Namespace) -> tuple[Profile, str]:
    text = Path(args.profiles).read_text()
    profile = parse_profile(text, ctx.target.name, str(args.profiles))
    admin = profile.admin_root.resolve()
    assert not (ctx.target.path.resolve().is_relative_to(admin)
                or ctx.result_dir.resolve().is_relative_to(admin)), \
        f'staging and results: expected outside admin_root {admin}, measured ' \
        f'{ctx.target.path}, {ctx.result_dir}'
    return profile, text


def worker_job(ctx: RunContext, args: argparse.Namespace, name: str, data_root: Path,
               token: str, profile_text: str) -> dict:
    return {
        'name': name, 'target': ctx.target.name, 'staging': str(ctx.target.path),
        'work_dir': str(ctx.work_dir / name), 'data_root': str(data_root), 'token': token,
        'profile': profile_text,
        'idle_sample_s': args.idle_sample_s,
        'resource_sample_s': args.resource_sample_s,
        'command_timeout_s': args.command_timeout_s,
        'lifecycle_timeout_s': args.lifecycle_timeout_s,
        'fixture_timeout_s': args.fixture_timeout_s,
    }


def start(ctx: RunContext, args: argparse.Namespace, profile: Profile,
          profile_text: str) -> Experiment:
    """Place the clients and start one lifecycle worker per participating machine."""
    # Every machine reaches the same data through its own admin view.
    data_root = profile.admin_root / 'agent-fs-bench' / 'mount-scale' / ctx.run_id
    assert not ctx.result_dir.resolve().is_relative_to(data_root.resolve()), \
        f'results: expected outside the measured data {data_root}, measured {ctx.result_dir}'
    placement = cluster.place(ctx.pool.nodes, args.tasks)
    nodes = cluster.machines(placement)
    if args.idle_mounts:
        assert len(nodes) == 1, \
            f'--idle-mounts machines: expected 1 participating host, measured {len(nodes)}'
    launcher = ctx.pool.launcher
    tag = f'{ctx.run_id}-{ctx.target.name}'
    engine = (Mdtest(args.mdtest, launcher, tag, args.job_timeout_s, args.startup_timeout_s)
              if args.workload in MDTEST_WORKLOADS else None)
    # run_id is unique per run and identical on every machine: the admin-view sentinel.
    workers = [Worker(node, worker_job(ctx, args, node.name, data_root, ctx.run_id,
                                       profile_text),
                      ctx.result_dir / 'hosts' / node.name, args.command_timeout_s,
                      launcher.connect_timeout_s) for node in nodes]
    return Experiment(profile.topology, profile, data_root, placement, workers, engine,
                      launcher)


def prepare(ctx: RunContext, args: argparse.Namespace, experiment: Experiment) -> dict[str, Any]:
    """Create the private data root and record the selected workload's boundary."""
    info: dict[str, Any] = {'placement': cluster.placement_record(experiment.placement),
                            'workload': args.workload, 'idle_mounts': args.idle_mounts}
    if args.workload in MDTEST_WORKLOADS:
        digests = cluster.binary_digests(cluster.machines(experiment.placement),
                                         ctx.pool.launcher, args.mdtest)
        info['mdtest'] = {'sha256': cluster.validate_binary(args.mdtest, digests),
                          'version': MDTEST_VERSION, 'workload': args.workload}
    experiment.workers[0].call('init-data')
    if args.workload == 'mount':
        info.update(mount_timing='rank-wrapper-invocation-to-ready-return',
                    cache_control='not-applicable-mount-lifecycle',
                    completion='site-mount-wrapper-ready-return',
                    durability='mount-ready')
    elif args.workload == 'mdtest-create':
        info.update(cache_control='not-applicable-zero-byte-create',
                    completion='native-mdtest-create-phase-end-barrier',
                    durability='not-applicable-zero-byte-files')
    else:
        assert args.workload == 'mdtest-io', f'unexpected workload: {args.workload}'
        info.update(cache_control='read-after-create-no-cache-drop',
                    completion='native-mdtest-phase-end-barrier',
                    durability='mdtest-y-per-file-fsync-no-directory-fsync')
    return info


# One repeat: run → validate → write ----------------------------------------------------------

def setup_clients(experiment: Experiment, repeat: int) -> tuple[list[Rank], list[dict]]:
    shares, count = cluster.placement_record(experiment.placement), len(experiment.placement)
    with ThreadPoolExecutor(max_workers=len(experiment.workers)) as pool:
        setups = list(pool.map(lambda worker: worker.call(
            'setup', repeat=repeat, clients=shares[worker.name], total_clients=count),
            experiment.workers))
    ranks = [Rank(worker.node, Path(client['workspace']))
             for worker, setup in zip(experiment.workers, setups) for client in setup['clients']]
    return ranks, [setup['baseline'] for setup in setups]


def timed(workers: list[Worker], action: str) -> tuple[list[dict], float]:
    """Replies of every worker and D_a, the controller's dispatch time for `action`."""
    started = time.perf_counter()
    replies = parallel(workers, action)
    return replies, time.perf_counter() - started


def measure_mount_repeat(ctx: RunContext, args: argparse.Namespace,
                         experiment: Experiment, number: int) -> Repeat:
    """One synchronized site-wrapper mount per active rank, then verify and unmount."""
    name = f'N{len(experiment.placement)}-R{number}'
    print(f'  {experiment.topology} {name}, workload mount, '
          f'N={len(experiment.placement)}, hosts={len(experiment.workers)}', flush=True)
    diagnostics = ctx.result_dir / name
    diagnostics.mkdir()
    workers = experiment.workers
    shares = cluster.placement_record(experiment.placement)
    ranks, baselines = setup_clients(experiment, number)
    plans = parallel(workers, 'mount-plan')
    commands: list[str] = []
    for worker, plan in zip(workers, plans):
        expected = shares[worker.name]
        assert len(plan['commands']) == expected, \
            f'{worker.name} mount commands: expected {expected}, measured {len(plan["commands"])}'
        commands.extend(plan['commands'])
    assert len(commands) == len(ranks), \
        f'mount commands: expected {len(ranks)}, measured {len(commands)}'
    programs = [[rank.node.python, '-u', '-m',
                 'agent_fs_bench.benchmarks.mount_scale_runner', 'mount',
                 '--command-file', path, '--timeout', str(args.command_timeout_s)]
                for rank, path in zip(ranks, commands)]
    run = cluster.run_program(tuple(rank.node for rank in ranks), experiment.launcher,
                              programs, diagnostics / 'measured', 'mount',
                              timeout_s=args.job_timeout_s,
                              startup_timeout_s=args.startup_timeout_s)
    cluster.validate_exit(run)
    elapsed = parse_mount_result(run.stdout, len(ranks))
    verified = parallel(workers, 'verify-mount')
    mounted = parallel(workers, 'idle')
    dispatch_s = {}
    unmounts, dispatch_s['unmount'] = timed(workers, 'unmount')
    released = parallel(workers, 'idle')
    mounts, offset = [], 0
    for worker, verification in zip(workers, verified):
        values = elapsed[offset:offset + shares[worker.name]]
        offset += len(values)
        mounts.append({'mount_elapsed_s': max(values),
                       'measured_mounts': verification['measured_mounts'],
                       'agent_elapsed_s': values})
    assert offset == len(elapsed), \
        f'mount rank times: expected {len(elapsed)}, assigned {offset}'
    return Repeat(number, name, baselines, mounted, [None] * len(workers), released,
                  mounts, unmounts, dispatch_s, [], [rank.node.name for rank in ranks])


def measure_mdtest_repeat(ctx: RunContext, args: argparse.Namespace,
                          experiment: Experiment, number: int) -> Repeat:
    """Native mdtest on one real mount per active client; no remount or fixture."""
    name = f'N{len(experiment.placement)}-R{number}'
    print(f'  {experiment.topology} {name}, workload {args.workload}, '
          f'N={len(experiment.placement)}, hosts={len(experiment.workers)}', flush=True)
    diagnostics = ctx.result_dir / name
    diagnostics.mkdir()
    workers, engine = experiment.workers, experiment.engine
    assert engine is not None, f'{args.workload}: expected an mdtest engine'
    ranks, baselines = setup_clients(experiment, number)
    mounts, dispatch_s = [], {}
    mounts, dispatch_s['mount'] = timed(workers, 'mount')
    mounted = parallel(workers, 'idle')
    parallel(workers, 'sample-start')
    operations = len(ranks) * args.files
    if args.workload == 'mdtest-create':
        timings = engine.create_timings(ranks, args.files, diagnostics / 'measured')
        phases = [Phase('file-create', operations, timings.phase_s['File creation'],
                        timings.rank_s['File creation'])]
    else:
        timings = engine.native_io_timings(ranks, args.files, args.file_size_bytes,
                                           diagnostics / 'measured')
        phases = [Phase(CREATE_WRITE, operations, timings.phase_s['File creation'],
                        timings.rank_s['File creation']),
                  Phase(READ, operations, timings.phase_s['File read'],
                        timings.rank_s['File read'])]
    workload = parallel(workers, 'sample-stop')
    unmounts, dispatch_s['unmount'] = timed(workers, 'unmount')
    released = parallel(workers, 'idle')
    return Repeat(number, name, baselines, mounted, workload, released, mounts, unmounts,
                  dispatch_s, phases, [rank.node.name for rank in ranks])


def measure_repeat(ctx: RunContext, args: argparse.Namespace, experiment: Experiment,
                   number: int) -> Repeat:
    if args.workload == 'mount':
        return measure_mount_repeat(ctx, args, experiment, number)
    assert args.workload in MDTEST_WORKLOADS, f'unexpected workload: {args.workload}'
    return measure_mdtest_repeat(ctx, args, experiment, number)


def validate_repeat(experiment: Experiment, result: Repeat) -> None:
    """Every placed client answered with its own alias (setup replies, not the plan)."""
    expected = len(experiment.placement)
    assert len(result.hosts) == expected, \
        f'{result.name} clients: expected {expected}, measured {len(result.hosts)}'


def write_phases(ctx: RunContext, keys: dict[str, Any], phases: list[Phase], hosts: list[str],
                 file_size_bytes: int) -> None:
    tasks = keys['tasks']
    for phase in phases:
        phase_keys = {**keys, 'phase': phase.name}
        ctx.emit('phases', phase_keys, phase_metrics(phase, tasks))
        if phase.name in DATA_PHASES:
            ctx.emit('bandwidth', phase_keys, bandwidth_metrics(phase, file_size_bytes))
        for sample, elapsed in enumerate(phase.rank_elapsed_s):
            ctx.emit('ranks', {**phase_keys, 'sample': sample, 'host': hosts[sample]},
                     {'elapsed_s': elapsed})


def write_lifecycle(ctx: RunContext, keys: dict[str, Any], host: str, action: str,
                    reply: dict) -> None:
    host_keys = {**keys, 'host': host}
    if action == 'mount':
        ctx.emit('host_mounts', host_keys, {'elapsed_s': reply['mount_elapsed_s'],
                                            'measured_mounts': reply['measured_mounts']})
    else:
        ctx.emit('host_unmounts', host_keys, {'elapsed_s': reply['unmount_elapsed_s'],
                                              'remaining_mounts': reply['remaining_mounts']})
    for sample, elapsed in enumerate(reply['agent_elapsed_s']):
        ctx.emit('agents', {**host_keys, 'action': action, 'sample': sample},
                 {'elapsed_s': elapsed})


def write_resources(ctx: RunContext, keys: dict[str, Any], host: str, baseline: dict,
                    mounted: dict, released: dict, workload: dict | None) -> None:
    host_keys = {**keys, 'host': host}
    for state, sample in (('baseline', baseline), ('mounted-idle', mounted),
                          ('released-idle', released)):
        ctx.emit('resource_levels', {**host_keys, 'state': state}, level_metrics(sample))
    for state, sample in (('mounted-idle', mounted), ('released-idle', released)):
        ctx.emit('resource_deltas', {**host_keys, 'state': state},
                 delta_metrics(sample, baseline))
    if workload is not None:
        ctx.emit('resource_workload', host_keys, workload_metrics(workload))


def write_repeat(ctx: RunContext, args: argparse.Namespace, experiment: Experiment,
                 result: Repeat) -> None:
    keys = {'topology': experiment.topology, 'tasks': len(experiment.placement),
            'repeat': result.number, 'workload': args.workload,
            'idle_mounts': args.idle_mounts}
    for action, elapsed in result.dispatch_s.items():
        ctx.emit('dispatch', {**keys, 'action': action}, {'controller_dispatch_elapsed_s': elapsed})
    for index, worker in enumerate(experiment.workers):
        for action, replies in (('mount', result.mounts), ('unmount', result.unmounts)):
            if replies:
                write_lifecycle(ctx, keys, worker.name, action, replies[index])
        write_resources(ctx, keys, worker.name, result.baselines[index], result.mounted[index],
                        result.released[index], result.workload[index])
    write_phases(ctx, keys, result.phases, result.hosts, args.file_size_bytes)


def mount_idle(ctx: RunContext, args: argparse.Namespace,
               experiment: Experiment) -> dict[str, Any] | None:
    """Mount the run-level idle cohort once, before active repeats, and record it."""
    if not args.idle_mounts:
        return None
    assert args.workload in ('mount', 'mdtest-create'), \
        f'idle mounts: expected workload mount or mdtest-create, measured {args.workload}'
    assert len(experiment.workers) == 1, \
        f'idle mounts hosts: expected 1, measured {len(experiment.workers)}'
    worker = experiment.workers[0]
    reply = worker.call('mount-idle', clients=args.idle_mounts)
    keys = {'topology': experiment.topology, 'tasks': len(experiment.placement),
            'idle_clients': args.idle_mounts, 'host': worker.name,
            'workload': args.workload}
    ctx.emit('idle_mounts', keys, {'elapsed_s': reply['mount_elapsed_s'],
                                   'measured_mounts': reply['measured_mounts']})
    ctx.info['idle_mounts_verified'] = reply['measured_mounts']
    return keys


def unmount_idle(ctx: RunContext, experiment: Experiment,
                 keys: dict[str, Any] | None) -> None:
    """Release the idle cohort after the last active repeat and verify no residue."""
    if keys is None:
        return
    reply = experiment.workers[0].call('unmount-idle')
    ctx.emit('idle_unmounts', keys, {'elapsed_s': reply['unmount_elapsed_s'],
                                     'remaining_mounts': reply['remaining_mounts']})


def clean_repeat(experiment: Experiment, result: Repeat) -> None:
    parallel(experiment.workers, 'cleanup')
    experiment.workers[0].call('remove-repeat', name=result.name)


def finish(experiment: Experiment) -> None:
    for worker in experiment.workers:
        worker.collect()
    experiment.workers[0].call('remove-data')
    for worker in experiment.workers:
        worker.close()


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    """A failure stops the run and leaves mounts and data for inspection; no unmount or retry."""
    profile, profile_text = load_profile(ctx, args)
    experiment = start(ctx, args, profile, profile_text)
    info = prepare(ctx, args, experiment)
    info['mount_profile_sha256'] = hashlib.sha256(profile_text.encode()).hexdigest()
    ctx.info.update(info)
    idle_keys = mount_idle(ctx, args, experiment)
    for number in range(1, args.repeats + 1):
        result = measure_repeat(ctx, args, experiment, number)
        validate_repeat(experiment, result)
        clean_repeat(experiment, result)
        write_repeat(ctx, args, experiment, result)
    unmount_idle(ctx, experiment, idle_keys)
    finish(experiment)
