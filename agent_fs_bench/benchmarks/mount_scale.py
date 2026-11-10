"""Mount-scale: N client tasks over the --mode machines using global Open MPI mdtest jobs.

| Symbol | Definition |
| --- | --- |
| N | `tasks`: clients, one mdtest rank each, dealt round-robin over the machines |
| K | `directories`: per-client directories, one private workspace + K-1 shared resources |
| F, S | `files` per client and phase, `file_size_bytes` |
| T_p | Aggregate mdtest phase time (maximum client time including the end barrier) |
| X_p | `ops_per_s = N*F / T_p`; never summed per machine |
| D_a | `controller_dispatch_elapsed_s`: controller time from notifying every worker of action a (mount/unmount) to receiving every reply |

Per repeat: setup → mount → mounted-idle sample → **warm-up** (one stat+read job per shared
resource, timings discarded) → **sampling** (private create/stat/read/remove job, then one
stat/read job per shared resource, with per-machine resource sampling) → unmount →
released-idle sample → validation → tables. Each table has one observation granularity:

| Table | One row per |
| --- | --- |
| `phases` | repeat × phase: ops, elapsed_s, ops_per_s, ops_per_s_per_task, rank_mean_us_per_op |
| `bandwidth` | repeat × create/read phase: bytes, elapsed_s, mib_per_s |
| `ranks` | repeat × phase × client: elapsed_s |
| `dispatch` | repeat × action: controller_dispatch_elapsed_s |
| `host_mounts`, `host_unmounts` | repeat × machine: elapsed_s, measured_mounts / remaining_mounts |
| `agents` | repeat × machine × action × client: elapsed_s |
| `resource_levels` | repeat × machine × idle state: raw levels and sampling interval |
| `resource_deltas` | repeat × machine × mounted/released-idle state: level − baseline |
| `resource_workload` | repeat × machine: sampling interval, peaks and peak − start |
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .mount_scale_cluster import Worker, parallel
from .mount_scale_mdtest import VERSION as MDTEST_VERSION, Mdtest, Phase, Rank
from .mount_scale_mounts import Profile, parse_profile

CONFIG = {
    'profiles': PROJECT_ROOT / 'local' / 'mounts.toml',
    # One N per run, dealt round-robin over the --mode machines; N above the CPU count
    # oversubscribes them.
    'tasks': 64,
    'directories': 3,  # K: one private workspace + K-1 shared resources per client.
    'files': 1000,
    'file_size_bytes': 4096,
    'repeats': 3,
    'mdtest': 'mdtest',  # Same binary on every machine; prefer an absolute path.
    'command_timeout_s': 300,
    'idle_sample_s': 1.0,
    'resource_sample_s': 0.1,
}

FIGURES = [
    Figure('phases', 'tasks', 'ops_per_s', split=('topology', 'phase'),
           xlabel='Tasks', ylabel='Throughput (Kops/s)', yscale=1000,
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
DATA_PHASES = ('/create', '/read')


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory-smoke', action='store_true',
                        help='Ordinary directories only on one machine; N=2, 32 files, one '
                             'repeat; never read mount profiles or execute mount/unmount.')
    options = parser.parse_args(argv)
    values = dict(CONFIG)
    if options.directory_smoke:
        values.update(tasks=2, files=32, repeats=1, idle_sample_s=0.1)
    return argparse.Namespace(**values, directory_smoke=options.directory_smoke)


# Metrics (pure) ------------------------------------------------------------------------------

def phase_metrics(phase: Phase, tasks: int) -> dict[str, float]:
    rate = phase.operations / phase.elapsed_s
    return {'ops': phase.operations, 'elapsed_s': phase.elapsed_s, 'ops_per_s': rate,
            'ops_per_s_per_task': rate / tasks,
            'rank_mean_us_per_op': sum(phase.rank_elapsed_s) / phase.operations * 1e6}


def bandwidth_metrics(phase: Phase, file_size_bytes: int) -> dict[str, float]:
    """Logical create/read bytes; shared data is read N times, not physical bytes."""
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
    profile: Profile | None
    data_root: Path
    placement: cluster.Placement
    workers: list[Worker]
    engine: Mdtest


@dataclass(frozen=True)
class Repeat:
    """Raw samples of one repeat; mount fields are empty without a mount profile."""

    number: int
    name: str
    baselines: list[dict]
    mounted: list[dict]
    workload: list[dict]
    released: list[dict]
    mounts: list[dict]
    unmounts: list[dict]
    dispatch_s: dict[str, float]
    phases: list[Phase]
    hosts: list[str]


def load_profile(ctx: RunContext, args: argparse.Namespace) -> tuple[Profile | None, str | None]:
    if args.directory_smoke:
        return None, None
    text = Path(args.profiles).read_text()
    profile = parse_profile(text, ctx.target.name, str(args.profiles))
    admin = profile.admin_root.resolve()
    assert not (ctx.target.path.resolve().is_relative_to(admin)
                or ctx.result_dir.resolve().is_relative_to(admin)), \
        f'staging and results: expected outside admin_root {admin}, measured ' \
        f'{ctx.target.path}, {ctx.result_dir}'
    return profile, text


def worker_job(ctx: RunContext, args: argparse.Namespace, name: str, data_root: Path,
               token: str, profile_text: str | None) -> dict:
    return {
        'name': name, 'target': ctx.target.name, 'staging': str(ctx.target.path),
        'work_dir': str(ctx.work_dir / name), 'data_root': str(data_root), 'token': token,
        'profile': profile_text, 'directory_smoke': args.directory_smoke,
        'directories': args.directories, 'idle_sample_s': args.idle_sample_s,
        'resource_sample_s': args.resource_sample_s,
        'command_timeout_s': args.command_timeout_s,
    }


def start(ctx: RunContext, args: argparse.Namespace, profile: Profile | None,
          profile_text: str | None) -> Experiment:
    """Place the clients and start one lifecycle worker per participating machine."""
    # Every machine reaches the same data through its own admin view; the directory
    # smoke keeps it in the staging work directory, so it runs on one machine only.
    data_root = (ctx.work_dir / 'data' if profile is None else
                 profile.admin_root / 'agent-fs-bench' / 'mount-scale' / ctx.run_id)
    assert not ctx.result_dir.resolve().is_relative_to(data_root.resolve()), \
        f'results: expected outside the measured data {data_root}, measured {ctx.result_dir}'
    placement = cluster.place(ctx.pool.nodes, args.tasks)
    nodes = cluster.machines(placement)
    if profile is None:
        assert len(nodes) == 1, f'--directory-smoke machines: expected 1, measured {len(nodes)}'
    launcher = ctx.pool.launcher
    # run_id is unique per run and identical on every machine: the admin-view sentinel.
    workers = [Worker(node, worker_job(ctx, args, node.name, data_root, ctx.run_id,
                                       profile_text),
                      ctx.result_dir / 'hosts' / node.name, args.command_timeout_s,
                      launcher.connect_timeout_s) for node in nodes]
    return Experiment('directory-smoke' if profile is None else profile.topology, profile,
                      data_root, placement, workers,
                      Mdtest(args.mdtest, launcher, f'{ctx.run_id}-{ctx.target.name}'))


def prepare(ctx: RunContext, args: argparse.Namespace, experiment: Experiment) -> dict[str, Any]:
    """Validate the binary and create the shared fixtures once, before any repeat."""
    digests = cluster.binary_digests(cluster.machines(experiment.placement), ctx.pool.launcher,
                                     args.mdtest)
    sha256 = cluster.validate_binary(args.mdtest, digests)
    experiment.workers[0].call('init-data')
    shared = tuple(experiment.data_root / f'shared-{number}'
                   for number in range(1, args.directories))
    experiment.engine.prepare(experiment.placement[0], shared, args.files, args.file_size_bytes,
                              ctx.result_dir / 'prepare')
    return {'mdtest': {'sha256': sha256, 'version': MDTEST_VERSION},
            'placement': cluster.placement_record(experiment.placement)}


# One repeat: run → validate → write ----------------------------------------------------------

def setup_clients(experiment: Experiment, repeat: int) -> tuple[list[Rank], list[dict]]:
    shares, count = cluster.placement_record(experiment.placement), len(experiment.placement)
    with ThreadPoolExecutor(max_workers=len(experiment.workers)) as pool:
        setups = list(pool.map(lambda worker: worker.call(
            'setup', repeat=repeat, clients=shares[worker.name], total_clients=count),
            experiment.workers))
    ranks = [Rank(worker.node, Path(client['workspace']),
                  tuple(Path(path) for path in client['shared']))
             for worker, setup in zip(experiment.workers, setups) for client in setup['clients']]
    return ranks, [setup['baseline'] for setup in setups]


def timed(workers: list[Worker], action: str) -> tuple[list[dict], float]:
    """Replies of every worker and D_a, the controller's dispatch time for `action`."""
    started = time.perf_counter()
    replies = parallel(workers, action)
    return replies, time.perf_counter() - started


def measure_repeat(ctx: RunContext, args: argparse.Namespace, experiment: Experiment,
                   number: int) -> Repeat:
    name = f'N{len(experiment.placement)}-R{number}'
    print(f'  {experiment.topology} {name}, K={args.directories}, '
          f'hosts={len(experiment.workers)}', flush=True)
    diagnostics = ctx.result_dir / name
    diagnostics.mkdir()
    workers, engine = experiment.workers, experiment.engine
    ranks, baselines = setup_clients(experiment, number)
    mounts, dispatch_s = [], {}
    if experiment.profile is not None:
        mounts, dispatch_s['mount'] = timed(workers, 'mount')
    mounted = parallel(workers, 'idle')
    engine.warm(ranks, args.files, args.file_size_bytes, diagnostics / 'warm')
    parallel(workers, 'sample-start')
    phases = engine.measure(ranks, args.files, args.file_size_bytes, diagnostics / 'measured')
    workload = parallel(workers, 'sample-stop')
    unmounts = []
    if experiment.profile is not None:
        unmounts, dispatch_s['unmount'] = timed(workers, 'unmount')
    released = parallel(workers, 'idle')
    return Repeat(number, name, baselines, mounted, workload, released, mounts, unmounts,
                  dispatch_s, phases, [rank.node.name for rank in ranks])


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
        if phase.name.endswith(DATA_PHASES):
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
                    mounted: dict, released: dict, workload: dict) -> None:
    host_keys = {**keys, 'host': host}
    for state, sample in (('baseline', baseline), ('mounted-idle', mounted),
                          ('released-idle', released)):
        ctx.emit('resource_levels', {**host_keys, 'state': state}, level_metrics(sample))
    for state, sample in (('mounted-idle', mounted), ('released-idle', released)):
        ctx.emit('resource_deltas', {**host_keys, 'state': state},
                 delta_metrics(sample, baseline))
    ctx.emit('resource_workload', host_keys, workload_metrics(workload))


def write_repeat(ctx: RunContext, args: argparse.Namespace, experiment: Experiment,
                 result: Repeat) -> None:
    keys = {'topology': experiment.topology, 'tasks': len(experiment.placement),
            'repeat': result.number}
    for action, elapsed in result.dispatch_s.items():
        ctx.emit('dispatch', {**keys, 'action': action}, {'controller_dispatch_elapsed_s': elapsed})
    for index, worker in enumerate(experiment.workers):
        for action, replies in (('mount', result.mounts), ('unmount', result.unmounts)):
            if replies:
                write_lifecycle(ctx, keys, worker.name, action, replies[index])
        write_resources(ctx, keys, worker.name, result.baselines[index], result.mounted[index],
                        result.released[index], result.workload[index])
    write_phases(ctx, keys, result.phases, result.hosts, args.file_size_bytes)


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
    if profile_text is not None:
        info['mount_profile_sha256'] = hashlib.sha256(profile_text.encode()).hexdigest()
    ctx.info.update(info)
    for number in range(1, args.repeats + 1):
        result = measure_repeat(ctx, args, experiment, number)
        validate_repeat(experiment, result)
        write_repeat(ctx, args, experiment, result)
        clean_repeat(experiment, result)
    finish(experiment)
