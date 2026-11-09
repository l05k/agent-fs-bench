"""Local-first, CPU-filled client scaling using a global Open MPI mdtest job."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import platform
import secrets
import shutil
import sys
import time

from agent_fs_bench.analysis import Figure
from agent_fs_bench.results import PROJECT_ROOT, RunContext
from agent_fs_bench.ssh import load_hosts, openmpi_agent
from .mount_scale_mdtest import Mdtest, Rank
from .mount_scale_mounts import load_profile
from .mount_scale_cluster import Worker, allocate, parallel

CONFIG = {
    'profiles': PROJECT_ROOT / 'local' / 'mounts.toml',
    'hosts': PROJECT_ROOT / 'local' / 'hosts.toml',
    'clients': None,  # One N per run; None fills every listed host's available CPUs.
    'directories': 3,  # Total per agent: one private workspace + X-1 shared resources.
    'files': 1000,
    'file_size_bytes': 4096,
    'repeats': 3,
    'mdtest': 'mdtest',
    'mpi_launcher': ('mpiexec',),  # Open MPI; host/rank placement is generated.
    'connect_timeout_s': 15,
    'command_timeout_s': 300,
    'idle_sample_s': 1.0,
    'resource_sample_s': 0.1,
}

FIGURES = [
    Figure('phases', 'clients', 'ops_per_s', split=('topology', 'phase'),
           xlabel='Client processes', ylabel='Throughput (Kops/s)', yscale=1000,
           logx=True, name='throughput'),
    Figure('mounts', 'clients', 'controller_dispatch_elapsed_s', split=('topology', 'action'),
           xlabel='Client processes', ylabel='Controller dispatch time (s)',
           logx=True, name='mount-dispatch'),
    Figure('resources', 'clients', 'used_bytes_delta', where={'state': 'mounted-idle'},
           split=('topology',), xlabel='Client processes', ylabel='Memory delta (MiB)',
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
                        help='Local ordinary directories only; N=2, 32 files, one repeat; '
                             'never read host/mount profiles or execute mount/unmount.')
    options = parser.parse_args(argv)
    values = dict(CONFIG)
    if options.directory_smoke:
        values.update(clients=2, files=32, repeats=1, idle_sample_s=0.1)
    return argparse.Namespace(**values, directory_smoke=options.directory_smoke)

def _validate(args: argparse.Namespace) -> None:
    if platform.system() != 'Linux':
        raise ValueError('mount-scale requires a Linux controller and Open MPI')
    if args.directory_smoke:
        available = len(os.sched_getaffinity(0))
        if available < 2:
            raise ValueError('--directory-smoke requires at least 2 available logical CPUs '
                             f'in the CPU affinity set; found {available}')

def _worker_job(ctx: RunContext, args: argparse.Namespace, name: str,
                data_root: Path, token: str) -> dict:
    return {
        'name': name, 'target': ctx.target.name, 'staging': str(ctx.target.path),
        'work_dir': str(ctx.work_dir), 'data_root': str(data_root), 'token': token,
        'deployment': str(ctx.work_dir / 'workers' / name),
        'profiles': str(args.profiles), 'directory_smoke': args.directory_smoke,
        'mdtest': args.mdtest, 'mpi_launcher': list(args.mpi_launcher),
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

def _emit_host(ctx: RunContext, topology: str, clients: int, host: str, repeat: int,
               baseline: dict, mounted: dict, released: dict, workload: dict,
               mount: dict | None, unmount: dict | None) -> None:
    for action, sample in (('mount', mount), ('unmount', unmount)):
        if sample is None:
            continue
        ctx.emit('lifecycle',
                 {'topology': topology, 'clients': clients, 'host': host,
                  'action': action, 'repeat': repeat},
                 {'elapsed_s': sample['mount_elapsed_s' if action == 'mount'
                                    else 'unmount_elapsed_s'],
                  'measured_mounts': sample.get('measured_mounts'),
                  'remaining_mounts': sample.get('remaining_mounts')})
        for index, elapsed in enumerate(sample['agent_elapsed_s']):
            ctx.emit('agents',
                     {'topology': topology, 'clients': clients, 'host': host,
                      'action': action, 'repeat': repeat, 'sample': index},
                     {'elapsed_s': elapsed})
    for state, sample in (('baseline', baseline), ('mounted-idle', mounted),
                          ('released-idle', released), ('workload', workload)):
        ctx.emit('resources',
                 {'topology': topology, 'clients': clients, 'host': host,
                  'state': state, 'repeat': repeat},
                 _resource_metrics(sample, state, baseline))

def _emit_phases(ctx: RunContext, topology: str, clients: int, repeat: int,
                 phases: list, mapping: list[dict], file_size_bytes: int) -> None:
    for phase in phases:
        rate = phase.operations / phase.elapsed_s
        created = phase.name.endswith(('/create', '/read'))
        byte_count = phase.operations * file_size_bytes if created else None
        ctx.emit('phases',
                 {'topology': topology, 'clients': clients, 'phase': phase.name,
                  'repeat': repeat},
                 {'ops': phase.operations, 'elapsed_s': phase.elapsed_s,
                  'ops_per_s': rate, 'ops_per_s_per_client': rate / clients,
                  'rank_mean_us_per_op':
                      sum(phase.rank_elapsed_s) / phase.operations * 1e6,
                  'bytes': byte_count,
                  'mib_per_s': (byte_count / phase.elapsed_s / (1024 * 1024)
                                if byte_count is not None else None)})
        for index, elapsed in enumerate(phase.rank_elapsed_s):
            ctx.emit('ranks',
                     {'topology': topology, 'clients': clients, 'phase': phase.name,
                      'repeat': repeat, 'sample': index,
                      'host': mapping[index]['host'], 'cpu': mapping[index]['cpu']},
                     {'elapsed_s': elapsed})

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    _validate(args)
    profile = None if args.directory_smoke else load_profile(args.profiles, ctx.target.name)
    if profile is not None:
        admin = profile.admin_root.resolve()
        if (ctx.target.path.resolve().is_relative_to(admin)
                or ctx.result_dir.resolve().is_relative_to(admin)):
            raise ValueError('staging and results must be outside admin_root')
    topology = 'directory-smoke' if profile is None else profile.topology
    data_root = (ctx.work_dir / 'data' if profile is None else
                 profile.admin_root / 'agent-fs-bench' / 'mount-scale' / ctx.run_id)
    if profile is not None and not data_root.resolve().is_relative_to(profile.admin_root.resolve()):
        raise ValueError('data_root escapes admin_root through a symlink')
    if ctx.result_dir.resolve().is_relative_to(data_root.resolve()):
        raise ValueError('results must be outside the measured filesystem data')
    # Missing private inventory is an error, not a silent local-only fallback.
    # Explicitly small N needs no SSH until the local affinity set is exhausted.
    local_capacity = len(os.sched_getaffinity(0))
    hosts = (() if args.directory_smoke or (args.clients is not None and args.clients <= local_capacity)
             else load_hosts(args.hosts))
    if any(host.name == 'local' for host in hosts):
        raise ValueError('SSH host name "local" is reserved for the controller')
    if len({host.address for host in hosts}) != len(hosts):
        raise ValueError('Open MPI host addresses must be unique')
    if any('@' in host.address or ':' in host.address for host in hosts):
        raise ValueError('Open MPI addresses must be hostnames or IPv4 addresses; use user/port fields')
    token = secrets.token_hex(16)
    workers: list[Worker] = []
    success = False
    try:
        workers.append(Worker('local', None,
                              _worker_job(ctx, args, 'local', data_root, token),
                              ctx.result_dir / 'hosts' / 'local', args.command_timeout_s,
                              args.connect_timeout_s))
        # Probe in inventory order, stopping once an explicit N has enough slots.
        # The None default deliberately includes the whole inventory.
        capacity = local_capacity
        for host in hosts:
            if args.clients is not None and capacity >= args.clients:
                break
            worker = Worker(host.name, host, _worker_job(ctx, args, host.name, data_root, token),
                            ctx.result_dir / 'hosts' / host.name, args.command_timeout_s,
                            args.connect_timeout_s)
            workers.append(worker)
            capacity += len(worker.info['cpus'])
        allocations = allocate([(worker.name, worker.info['cpus']) for worker in workers], args.clients)
        hostnames = [worker.info['hostname'] for worker in workers]
        if len(set(hostnames)) != len(hostnames):
            raise ValueError('duplicate execution hostnames: inventory must list distinct machines')
        identities = {(worker.info['mdtest_sha256'], worker.info['mpi_version'], worker.info['machine'])
                      for worker in workers}
        if len(identities) != 1:
            raise ValueError('all hosts must use the same mdtest binary, Open MPI version and architecture')
        count = sum(len(item.cpus) for item in allocations)
        launcher = list(args.mpi_launcher)
        if len(workers) > 1:
            launcher += ['--mca', 'plm_rsh_agent', openmpi_agent(args.hosts, args.connect_timeout_s),
                         '--mca', 'plm_rsh_no_tree_spawn', '1']
        engine = Mdtest(args.mdtest, launcher, args.command_timeout_s)
        data_root.mkdir(parents=True, exist_ok=False)
        (data_root / '.owner').write_text(token)
        shared = tuple(data_root / f'shared-{number}' for number in range(1, args.directories))
        for path in shared:
            path.mkdir()
        engine.prepare(shared, args.files, args.file_size_bytes, ctx.result_dir / 'prepare')
        ctx.info['hosts'] = [{'name': worker.name, **worker.info,
                              'assigned_cpus': list(allocation.cpus)}
                             for worker, allocation in zip(workers, allocations)]
        if profile is not None:
            ctx.info['mount_profile_sha256'] = hashlib.sha256(
                args.profiles.read_bytes()).hexdigest()
        ctx.info['rank_placement'] = {}
        for repeat in range(1, args.repeats + 1):
            name = f'N{count}-R{repeat}'
            print(f'  {topology} {name}, K={args.directories}, hosts={len(workers)}', flush=True)
            diagnostics = ctx.result_dir / name
            diagnostics.mkdir()
            setups = []
            with ThreadPoolExecutor(max_workers=len(workers)) as pool:
                futures = [pool.submit(worker.call, 'setup', repeat=repeat,
                                       clients=len(allocation.cpus), total_clients=count)
                           for worker, allocation in zip(workers, allocations)]
                setups = [future.result() for future in futures]
            ranks = []
            mapping = []
            for worker, allocation, setup in zip(workers, allocations, setups):
                if len(setup['clients']) != len(allocation.cpus):
                    raise RuntimeError(f'{worker.name}: aliases do not cover every allocated client')
                for cpu, client in zip(allocation.cpus, setup['clients']):
                    ranks.append(Rank(
                        host='localhost' if worker.host is None else worker.host.address,
                        hostname=worker.info['hostname'], cpu=cpu,
                        python=sys.executable if worker.host is None else worker.host.python,
                        package_root=worker.package_root, binary=worker.info['binary'],
                        workspace=Path(client['workspace']),
                        shared=tuple(Path(path) for path in client['shared']),
                    ))
                    mapping.append({'host': worker.name, 'cpu': cpu})
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
            ctx.info['rank_placement'][name] = {
                job: [{**proof, 'host': mapping[proof['index']]['host']} for proof in proofs]
                for job, proofs in placements.items()
            }
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
                         {'topology': topology, 'clients': count,
                          'action': 'mount', 'repeat': repeat},
                         {'controller_dispatch_elapsed_s': mount_dispatch_s,
                          'measured_mounts': sum(row['measured_mounts'] for row in mount),
                          'remaining_mounts': None})
                ctx.emit('mounts',
                         {'topology': topology, 'clients': count,
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
            shutil.rmtree(data_root / name)
        for worker in workers:
            worker.collect()
        shutil.rmtree(data_root)
        ctx.info.update(engine.metadata)
        success = True
    except BaseException:
        print(f'mount-scale failed; inspect each host under {ctx.work_dir} '
              f'and data under {data_root}; no automatic unmount or retry', flush=True)
        raise
    finally:
        for worker in workers:
            worker.close(success=success)
