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
    for name in ('directories', 'files', 'file_size_bytes', 'repeats', 'connect_timeout_s'):
        if type(getattr(args, name)) is not int or getattr(args, name) < 1:
            raise ValueError(f'CONFIG[{name!r}] must be a positive integer')
    if args.clients is not None and (type(args.clients) is not int or args.clients < 1):
        raise ValueError('clients must be None (all available CPUs) or one positive integer')
    for name in ('idle_sample_s', 'resource_sample_s', 'command_timeout_s'):
        if getattr(args, name) <= 0:
            raise ValueError(f'CONFIG[{name!r}] must be positive')


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


def _emit_host(ctx: RunContext, case: str, worker: Worker, repeat: int,
               baseline: dict, mounted: dict, released: dict, workload: dict,
               mount: dict | None, unmount: dict | None) -> None:
    prefix = f'{case}/hosts/{worker.name}'
    if mount is not None and unmount is not None:
        for label, sample in (('mount', mount), ('unmount', unmount)):
            metrics = {key: value for key, value in sample.items() if key != 'agent_elapsed_s'}
            ctx.emit(prefix + '/' + label, metrics, repeat=repeat,
                     details={'agent_elapsed_s': sample['agent_elapsed_s']})
    ctx.emit(prefix + '/resources/baseline', baseline, repeat=repeat)
    for label, sample in (('mounted-idle', mounted), ('released-idle', released)):
        metrics = dict(sample)
        for key in ('used_bytes', 'slab_bytes', 'slab_unreclaimable_bytes', 'mount_entries'):
            metrics[key + '_delta'] = sample[key] - baseline[key]
        ctx.emit(prefix + '/resources/' + label, metrics, repeat=repeat)
    ctx.emit(prefix + '/resources/workload', workload, repeat=repeat)


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
        ctx.parameters = {
            'topology': topology, 'directories_per_agent': args.directories,
            'clients': count, 'requested_clients': args.clients,
            'files_per_directory_per_agent': args.files,
            'file_size_bytes': args.file_size_bytes, 'repeats': args.repeats,
            'cache': 'shared-preread-every-client; private-create-then-read', 'fsync': False,
            'idle_sample_s': args.idle_sample_s, 'resource_sample_s': args.resource_sample_s,
            'placement_policy': 'local-first; inventory-order; one-rank-per-affinity-cpu',
            'hosts': [{'name': worker.name, **worker.info,
                       'assigned_cpus': list(allocation.cpus)}
                      for worker, allocation in zip(workers, allocations)],
        }
        if profile is not None:
            ctx.parameters['mount_profile_sha256'] = hashlib.sha256(args.profiles.read_bytes()).hexdigest()
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
                    mapping.append({'rank': len(mapping), 'host': worker.name, 'cpu': cpu})
            ctx.parameters['planned_rank_placement'] = mapping
            mount = unmount = None
            if profile is not None:
                started = time.perf_counter()
                mount = parallel(workers, 'mount')
                mount_dispatch_s = time.perf_counter() - started
            mounted = parallel(workers, 'idle')
            engine.warm(ranks, args.files, args.file_size_bytes, diagnostics / 'warm')
            parallel(workers, 'sample-start')
            phases, placements = engine.measure(ranks, args.files, args.file_size_bytes,
                                                diagnostics / 'measured')
            ctx.parameters['rank_placement'] = {
                job: [{'rank': proof['index'], 'host': mapping[proof['index']]['host'],
                       'hostname': proof['hostname'], 'cpu': proof['cpu'],
                       'affinity': proof['affinity']} for proof in proofs]
                for job, proofs in placements.items()
            }
            workload = parallel(workers, 'sample-stop')
            if profile is not None:
                started = time.perf_counter()
                unmount = parallel(workers, 'unmount')
                unmount_dispatch_s = time.perf_counter() - started
            released = parallel(workers, 'idle')
            ctx.parameters.update(engine.metadata)
            case = f'{topology}/N{count}'
            if mount is not None and unmount is not None:
                # Dispatch includes SSH control latency and mount verification. It is
                # intentionally not presented as pure mount command time.
                ctx.emit(case + '/mount', {
                    'controller_dispatch_elapsed_s': mount_dispatch_s,
                    'measured_mounts': sum(row['measured_mounts'] for row in mount),
                }, repeat=repeat)
                ctx.emit(case + '/unmount', {
                    'controller_dispatch_elapsed_s': unmount_dispatch_s,
                    'remaining_mounts': sum(row['remaining_mounts'] for row in unmount),
                }, repeat=repeat)
            for index, worker in enumerate(workers):
                _emit_host(ctx, case, worker, repeat, setups[index]['baseline'], mounted[index],
                           released[index], workload[index],
                           None if mount is None else mount[index],
                           None if unmount is None else unmount[index])
            for phase in phases:
                rate = phase.operations / phase.elapsed_s
                metrics = {
                    'ops': phase.operations, 'elapsed_s': phase.elapsed_s,
                    'ops_per_s': rate, 'ops_per_s_per_client': rate / count,
                    'rank_mean_us_per_op': sum(phase.rank_elapsed_s) / phase.operations * 1e6,
                }
                if phase.name.endswith(('/create', '/read')):
                    metrics['bytes'] = phase.operations * args.file_size_bytes
                    metrics['mib_per_s'] = metrics['bytes'] / phase.elapsed_s / (1024 * 1024)
                ctx.emit(case + '/' + phase.name, metrics, repeat=repeat,
                         details={'rank_elapsed_s': phase.rank_elapsed_s})
            parallel(workers, 'cleanup')
            shutil.rmtree(data_root / name)
        for worker in workers:
            worker.collect()
        shutil.rmtree(data_root)
        success = True
    except BaseException:
        print(f'mount-scale failed; inspect each host under {ctx.work_dir} '
              f'and data under {data_root}; no automatic unmount or retry', flush=True)
        raise
    finally:
        for worker in workers:
            worker.close(success=success)
