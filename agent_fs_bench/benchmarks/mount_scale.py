"""Mount-scale: N client tasks over the --mode machines using global Open MPI mdtest jobs."""

from __future__ import annotations

import argparse
import hashlib
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .mount_scale_cluster import Worker, parallel
from .mount_scale_mdtest import Mdtest, Rank
from .mount_scale_mounts import parse_profile

CONFIG = {
    'profiles': PROJECT_ROOT / 'local' / 'mounts.toml',
    # One N per run; None fills every logical CPU of the --mode machines, one client each.
    'tasks': None,
    'directories': 3,  # Total per agent: one private workspace + X-1 shared resources.
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
    Figure('mounts', 'tasks', 'controller_dispatch_elapsed_s', split=('topology', 'action'),
           xlabel='Tasks', ylabel='Controller dispatch time (s)',
           logx=True, name='mount-dispatch'),
    Figure('resources', 'tasks', 'used_bytes_delta', where={'state': 'mounted-idle'},
           split=('topology',), xlabel='Tasks', ylabel='Memory delta (MiB)',
           yscale=1024 * 1024, logx=True, name='memory-delta'),
]

_LEVELS = ('used_bytes', 'cached_bytes', 'dirty_bytes', 'slab_bytes',
           'slab_reclaimable_bytes', 'slab_unreclaimable_bytes', 'mount_entries')
_INTERVAL = ('sample_elapsed_s', 'cpu_busy_s', 'cpu_busy_cores', 'cpu_busy_percent')
_DELTAS = ('used_bytes', 'slab_bytes', 'slab_unreclaimable_bytes', 'mount_entries')
_PEAKS = ('used_bytes', 'cached_bytes', 'dirty_bytes', 'slab_bytes',
          'slab_reclaimable_bytes', 'slab_unreclaimable_bytes')

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

def _worker_job(ctx: RunContext, args: argparse.Namespace, name: str, data_root: Path,
                token: str, profile: str | None) -> dict:
    return {
        'name': name, 'target': ctx.target.name, 'staging': str(ctx.target.path),
        'work_dir': str(ctx.work_dir / name), 'data_root': str(data_root), 'token': token,
        'profile': profile, 'directory_smoke': args.directory_smoke,
        'directories': args.directories, 'idle_sample_s': args.idle_sample_s,
        'resource_sample_s': args.resource_sample_s,
        'command_timeout_s': args.command_timeout_s,
    }

def _resource_metrics(sample: dict, state: str, baseline: dict) -> dict:
    """One fixed column set for every state; inapplicable values stay empty."""
    metrics = {key: sample[key] if state != 'workload' else None for key in _LEVELS}
    metrics.update({key: sample[key] for key in _INTERVAL})
    metrics.update({
        key + '_delta': (sample[key] - baseline[key]
                         if state in ('mounted-idle', 'released-idle') else None)
        for key in _DELTAS
    })
    for key in _PEAKS:
        metrics['peak_' + key] = sample.get('peak_' + key)
        metrics['peak_' + key + '_delta'] = sample.get('peak_' + key + '_delta')
    return metrics

def _emit_host(ctx: RunContext, topology: str, tasks: int, host: str, repeat: int,
               baseline: dict, mounted: dict, released: dict, workload: dict,
               mount: dict | None, unmount: dict | None) -> None:
    for action, sample in (('mount', mount), ('unmount', unmount)):
        if sample is None:
            continue
        ctx.emit('lifecycle',
                 {'topology': topology, 'tasks': tasks, 'host': host,
                  'action': action, 'repeat': repeat},
                 {'elapsed_s': sample['mount_elapsed_s' if action == 'mount'
                                    else 'unmount_elapsed_s'],
                  'measured_mounts': sample.get('measured_mounts'),
                  'remaining_mounts': sample.get('remaining_mounts')})
        for index, elapsed in enumerate(sample['agent_elapsed_s']):
            ctx.emit('agents',
                     {'topology': topology, 'tasks': tasks, 'host': host,
                      'action': action, 'repeat': repeat, 'sample': index},
                     {'elapsed_s': elapsed})
    for state, sample in (('baseline', baseline), ('mounted-idle', mounted),
                          ('released-idle', released), ('workload', workload)):
        ctx.emit('resources',
                 {'topology': topology, 'tasks': tasks, 'host': host,
                  'state': state, 'repeat': repeat},
                 _resource_metrics(sample, state, baseline))

def _emit_phases(ctx: RunContext, topology: str, tasks: int, repeat: int,
                 phases: list, mapping: list[dict], file_size_bytes: int) -> None:
    for phase in phases:
        rate = phase.operations / phase.elapsed_s
        created = phase.name.endswith(('/create', '/read'))
        byte_count = phase.operations * file_size_bytes if created else None
        ctx.emit('phases',
                 {'topology': topology, 'tasks': tasks, 'phase': phase.name,
                  'repeat': repeat},
                 {'ops': phase.operations, 'elapsed_s': phase.elapsed_s,
                  'ops_per_s': rate, 'ops_per_s_per_task': rate / tasks,
                  'rank_mean_us_per_op':
                      sum(phase.rank_elapsed_s) / phase.operations * 1e6,
                  'bytes': byte_count,
                  'mib_per_s': (byte_count / phase.elapsed_s / (1024 * 1024)
                                if byte_count is not None else None)})
        for index, elapsed in enumerate(phase.rank_elapsed_s):
            ctx.emit('ranks',
                     {'topology': topology, 'tasks': tasks, 'phase': phase.name,
                      'repeat': repeat, 'sample': index,
                      'host': mapping[index]['host'], 'cpu': mapping[index]['cpu']},
                     {'elapsed_s': elapsed})

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    profile_bytes = None if args.directory_smoke else Path(args.profiles).read_bytes()
    profile_text = None if profile_bytes is None else profile_bytes.decode()
    profile = None if profile_text is None else parse_profile(profile_text, ctx.target.name,
                                                              str(args.profiles))
    if profile is not None:
        admin = profile.admin_root
        if (ctx.target.path.resolve().is_relative_to(admin)
                or ctx.result_dir.resolve().is_relative_to(admin)):
            raise ValueError('staging and results must be outside admin_root')
    topology = 'directory-smoke' if profile is None else profile.topology
    # Every machine reaches the same data through its own admin view; the directory
    # smoke keeps it in the staging work directory, so it runs on one machine only.
    data_root = (ctx.work_dir / 'data' if profile is None else
                 profile.admin_root / 'agent-fs-bench' / 'mount-scale' / ctx.run_id)
    if ctx.result_dir.resolve().is_relative_to(data_root.resolve()):
        raise ValueError('results must be outside the measured filesystem data')
    # One single-threaded client per logical CPU, never oversubscribed.
    slots = cluster.allocate(ctx.pool.nodes, args.tasks, 1, 1)
    nodes = list(dict.fromkeys(slot.node for slot in slots))
    if profile is None and len(nodes) > 1:
        raise ValueError('--directory-smoke keeps its data in one machine\'s staging and runs on '
                         f'one machine only; its {len(slots)} tasks span '
                         f'{", ".join(node.name for node in nodes)}')
    count = len(slots)
    token = secrets.token_hex(16)
    workers: list[Worker] = []
    success = False
    try:
        for node in nodes:
            workers.append(Worker(node, _worker_job(ctx, args, node.name, data_root, token,
                                                    profile_text),
                                  ctx.result_dir / 'hosts' / node.name, args.command_timeout_s))
        engine = Mdtest(args.mdtest, args.command_timeout_s, f'{ctx.run_id}-{ctx.target.name}')
        workers[0].call('init-data')
        shared = tuple(data_root / f'shared-{number}' for number in range(1, args.directories))
        engine.prepare(slots[0], shared, args.files, args.file_size_bytes,
                       ctx.result_dir / 'prepare')
        ctx.info['hosts'] = {node.name: {'assigned_cpus': [cpu for slot in slots
                                                           if slot.node is node
                                                           for cpu in slot.cpus]}
                             for node in nodes}
        if profile_bytes is not None:
            ctx.info['mount_profile_sha256'] = hashlib.sha256(profile_bytes).hexdigest()
        ctx.info['placement'] = {}
        for repeat in range(1, args.repeats + 1):
            name = f'N{count}-R{repeat}'
            print(f'  {topology} {name}, K={args.directories}, hosts={len(workers)}', flush=True)
            diagnostics = ctx.result_dir / name
            diagnostics.mkdir()
            local_slots = [[slot for slot in slots if slot.node is worker.node]
                           for worker in workers]
            with ThreadPoolExecutor(max_workers=len(workers)) as pool:
                futures = [pool.submit(worker.call, 'setup', repeat=repeat,
                                       clients=len(node_slots), total_clients=count)
                           for worker, node_slots in zip(workers, local_slots)]
                setups = [future.result() for future in futures]
            ranks = []
            mapping = []
            for worker, node_slots, setup in zip(workers, local_slots, setups):
                if len(setup['clients']) != len(node_slots):
                    raise RuntimeError(f'{worker.name}: aliases do not cover every allocated client')
                for slot, client in zip(node_slots, setup['clients']):
                    ranks.append(Rank(slot, Path(client['workspace']),
                                      tuple(Path(path) for path in client['shared'])))
                    mapping.append({'host': worker.name, 'cpu': slot.cpus[0]})
            mount = unmount = None
            mount_dispatch_s = unmount_dispatch_s = None
            if profile is not None:
                started = time.perf_counter()
                mount = parallel(workers, 'mount')
                mount_dispatch_s = time.perf_counter() - started
            mounted = parallel(workers, 'idle')
            engine.warm(ranks, args.files, args.file_size_bytes, diagnostics / 'warm')
            parallel(workers, 'sample-start')
            phases, placements = engine.measure(ranks, args.files, args.file_size_bytes,
                                                diagnostics / 'measured')
            # Verified placement proofs per mdtest job; the planned host/CPU is in `ranks`.
            ctx.info['placement'][name] = placements
            workload = parallel(workers, 'sample-stop')
            if profile is not None:
                started = time.perf_counter()
                unmount = parallel(workers, 'unmount')
                unmount_dispatch_s = time.perf_counter() - started
            released = parallel(workers, 'idle')
            if mount is not None and unmount is not None:
                # Dispatch includes SSH control latency and mount verification. It is
                # intentionally not presented as pure mount command time.
                ctx.emit('mounts',
                         {'topology': topology, 'tasks': count,
                          'action': 'mount', 'repeat': repeat},
                         {'controller_dispatch_elapsed_s': mount_dispatch_s,
                          'measured_mounts': sum(row['measured_mounts'] for row in mount),
                          'remaining_mounts': None})
                ctx.emit('mounts',
                         {'topology': topology, 'tasks': count,
                          'action': 'unmount', 'repeat': repeat},
                         {'controller_dispatch_elapsed_s': unmount_dispatch_s,
                          'measured_mounts': None,
                          'remaining_mounts': sum(row['remaining_mounts'] for row in unmount)})
            for index, worker in enumerate(workers):
                _emit_host(ctx, topology, count, worker.name, repeat,
                           setups[index]['baseline'], mounted[index],
                           released[index], workload[index],
                           None if mount is None else mount[index],
                           None if unmount is None else unmount[index])
            _emit_phases(ctx, topology, count, repeat, phases, mapping, args.file_size_bytes)
            parallel(workers, 'cleanup')
            workers[0].call('remove-repeat', name=name)
        for worker in workers:
            worker.collect()
        workers[0].call('remove-data')
        ctx.info.update(engine.metadata)
        success = True
    except BaseException:
        print(f'mount-scale failed; inspect each machine under {ctx.work_dir} '
              f'and data under {data_root}; no automatic unmount or retry', flush=True)
        raise
    finally:
        for worker in workers:
            worker.close(success=success)
