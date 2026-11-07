"""Client scaling with one global Open MPI mdtest job, local or remote-only.

``local``: every rank runs on this controller, one per usable CPU.
``remote``: ranks run only on H homogeneous SSH hosts, one per usable CPU of
each host; the controller launches MPI and creates the shared fixture but
runs no rank and mounts no client.
"""

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
from .mount_scale_cluster import Worker, parallel, preflight

CONFIG = {
    'profiles': PROJECT_ROOT / 'local' / 'mounts.toml',
    'hosts': PROJECT_ROOT / 'local' / 'hosts.toml',
    'mode': 'local',  # 'local': ranks on this machine; 'remote': ranks on SSH hosts only.
    'clients': None,  # local: N ranks; None uses every usable CPU of this machine.
    'remote_hosts': None,  # remote: H, the first H inventory hosts; None uses all.
    'reserved_cpus': 4,  # Highest affinity CPUs per machine kept free of ranks (64 -> 60).
    'directories': 3,  # Total per agent: one private workspace + X-1 shared resources.
    'files': 1000,
    'file_size_bytes': 4096,
    'repeats': 3,
    'shared_seed': 1,  # mdtest -R --random-seed; each rank uses seed + rank.
    'mdtest': 'mdtest',
    'mpi_launcher': ('mpiexec',),  # Open MPI; host/rank placement is generated.
    'connect_timeout_s': 15,
    'mount_timeout_s': 300,  # Each mount/unmount command and worker control step.
    'mdtest_timeout_s': 900,  # Each global MPI job (warm-shared-i, shared-i, private).
    'idle_sample_s': 1.0,
    'resource_sample_s': 0.1,
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory-smoke', action='store_true',
                        help='Local ordinary directories only; N=2, 32 files, one repeat; '
                             'never read host/mount profiles or execute mount/unmount.')
    parser.add_argument('--idle-clients-per-host', type=int, default=0, metavar='M',
                        help='Also mount M never-accessed clients on every participating '
                             'machine (same profile and K as the active clients); default 0.')
    options = parser.parse_args(argv)
    if options.idle_clients_per_host < 0:
        parser.error('--idle-clients-per-host must be >= 0')
    if options.directory_smoke and options.idle_clients_per_host:
        parser.error('--idle-clients-per-host needs a mount profile, not --directory-smoke')
    values = dict(CONFIG)
    if options.directory_smoke:
        values.update(mode='local', clients=2, reserved_cpus=1,
                      files=32, repeats=1, idle_sample_s=0.1)
    return argparse.Namespace(**values, directory_smoke=options.directory_smoke,
                              idle_clients_per_host=options.idle_clients_per_host)


def _validate(args: argparse.Namespace) -> None:
    if platform.system() != 'Linux':
        raise ValueError('mount-scale requires a Linux controller and Open MPI')
    if args.mode not in ('local', 'remote'):
        raise ValueError("CONFIG['mode'] must be 'local' or 'remote'")
    for name in ('directories', 'files', 'file_size_bytes', 'repeats', 'connect_timeout_s',
                 'shared_seed'):
        if type(getattr(args, name)) is not int or getattr(args, name) < 1:
            raise ValueError(f'CONFIG[{name!r}] must be a positive integer')
    if type(args.reserved_cpus) is not int or args.reserved_cpus < 0:
        raise ValueError("CONFIG['reserved_cpus'] must be a non-negative integer")
    for name in ('clients', 'remote_hosts'):
        value = getattr(args, name)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f'CONFIG[{name!r}] must be None or one positive integer')
    for name in ('idle_sample_s', 'resource_sample_s', 'mount_timeout_s', 'mdtest_timeout_s'):
        if getattr(args, name) <= 0:
            raise ValueError(f'CONFIG[{name!r}] must be positive')
    if args.directory_smoke:
        available = len(os.sched_getaffinity(0))
        if available - args.reserved_cpus < 2:
            raise ValueError('--directory-smoke requires at least 2 usable logical CPUs '
                             f'after reserving {args.reserved_cpus}; found {available}')


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
        'mount_timeout_s': args.mount_timeout_s, 'reserved_cpus': args.reserved_cpus,
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


def _hosts(args: argparse.Namespace) -> tuple:
    hosts = load_hosts(args.hosts)
    if any(host.name == 'local' for host in hosts):
        raise ValueError('SSH host name "local" is reserved for the controller')
    if len({host.address for host in hosts}) != len(hosts):
        raise ValueError('Open MPI host addresses must be unique')
    if any('@' in host.address or ':' in host.address for host in hosts):
        raise ValueError('Open MPI addresses must be hostnames or IPv4 addresses; use user/port fields')
    count = len(hosts) if args.remote_hosts is None else args.remote_hosts
    if not 1 <= count <= len(hosts):
        raise ValueError(f'remote_hosts={args.remote_hosts} but the inventory lists {len(hosts)} hosts')
    return hosts[:count]


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
    remote = args.mode == 'remote'
    hosts = _hosts(args) if remote else ()
    token = secrets.token_hex(16)
    workers: list[Worker] = []
    success = False
    try:
        # The controller always creates the shared fixture, so it must match the hosts.
        # In remote mode it runs no rank, so no CPU reservation applies to it.
        controller = preflight({**_worker_job(ctx, args, 'local', data_root, token),
                                **({'reserved_cpus': 0} if remote else {})})
        if remote:
            for host in hosts:
                workers.append(Worker(host.name, host,
                                      _worker_job(ctx, args, host.name, data_root, token),
                                      ctx.result_dir / 'hosts' / host.name,
                                      args.mount_timeout_s, args.connect_timeout_s))
            per_host = {len(worker.info['cpus']) for worker in workers}
            if len(per_host) != 1:
                raise ValueError(f'remote hosts must be homogeneous; usable CPUs per host: {sorted(per_host)}')
            allocations = [worker.info['cpus'] for worker in workers]
            if controller['hostname'] in {worker.info['hostname'] for worker in workers}:
                raise ValueError('remote mode must not run ranks on the controller')
        else:
            usable = controller['cpus']
            if args.clients is not None and args.clients > len(usable):
                raise ValueError(f'clients={args.clients} exceeds {len(usable)} usable local CPUs '
                                 f'(after reserving {args.reserved_cpus})')
            workers.append(Worker('local', None,
                                  _worker_job(ctx, args, 'local', data_root, token),
                                  ctx.result_dir / 'hosts' / 'local',
                                  args.mount_timeout_s, args.connect_timeout_s))
            allocations = [usable[:len(usable) if args.clients is None else args.clients]]
        hostnames = [worker.info['hostname'] for worker in workers]
        if len(set(hostnames)) != len(hostnames):
            raise ValueError('duplicate execution hostnames: inventory must list distinct machines')
        identities = {(info['mdtest_sha256'], info['mpi_version'], info['machine'])
                      for info in [controller, *(worker.info for worker in workers)]}
        if len(identities) != 1:
            raise ValueError('all hosts must use the same mdtest binary, Open MPI version and architecture')
        count = sum(len(cpus) for cpus in allocations)
        launcher = list(args.mpi_launcher)
        if remote:
            launcher += ['--mca', 'plm_rsh_agent', openmpi_agent(args.hosts, args.connect_timeout_s),
                         '--mca', 'plm_rsh_no_tree_spawn', '1']
        engine = Mdtest(args.mdtest, launcher, args.mdtest_timeout_s, args.shared_seed)
        # Global rank 0 runs on workers[0]. When that host is remote, mdtest writes
        # the rank CSV in its staging and the controller fetches it over the worker.
        rank0 = workers[0]
        csv_dir = None if rank0.host is None else Path(rank0.job['work_dir']) / 'mdtest'
        read = None if rank0.host is None else (
            lambda path: rank0.call('read', path=str(path))['text'])
        data_root.mkdir(parents=True, exist_ok=False)
        (data_root / '.owner').write_text(token)
        shared = tuple(data_root / f'shared-{number}' for number in range(1, args.directories))
        for path in shared:
            path.mkdir()
        engine.prepare(shared, args.files, args.file_size_bytes, ctx.result_dir / 'prepare')
        idle_clients = args.idle_clients_per_host
        ctx.parameters = {
            'topology': topology, 'mode': args.mode, 'directories_per_agent': args.directories,
            'clients': count, 'requested_clients': args.clients,
            'remote_hosts': len(workers) if remote else 0,
            'clients_per_host': count // len(workers),
            'idle_clients_per_host': idle_clients, 'reserved_cpus_per_host': args.reserved_cpus,
            'files_per_directory_per_agent': args.files,
            'file_size_bytes': args.file_size_bytes, 'repeats': args.repeats,
            'cache': 'shared-preread-every-client-then-shared-first; private-create-then-read',
            'shared_access': f'mdtest -R --random-seed={args.shared_seed} (seed + rank)',
            'fsync': False,
            'idle_sample_s': args.idle_sample_s, 'resource_sample_s': args.resource_sample_s,
            'mount_timeout_s': args.mount_timeout_s, 'mdtest_timeout_s': args.mdtest_timeout_s,
            'placement_policy': f'{args.mode}; one-rank-per-usable-affinity-cpu',
            'controller': {key: controller[key] for key in ('hostname', 'kernel', 'machine')},
            'hosts': [{'name': worker.name, **worker.info, 'assigned_cpus': cpus}
                      for worker, cpus in zip(workers, allocations)],
        }
        if profile is not None:
            ctx.parameters['mount_profile_sha256'] = hashlib.sha256(args.profiles.read_bytes()).hexdigest()
        for repeat in range(1, args.repeats + 1):
            name = f'N{count}-R{repeat}'
            print(f'  {topology} {name}, K={args.directories}, M={idle_clients}, '
                  f'hosts={len(workers)}', flush=True)
            diagnostics = ctx.result_dir / name
            diagnostics.mkdir()
            with ThreadPoolExecutor(max_workers=len(workers)) as pool:
                futures = [pool.submit(worker.call, 'setup', repeat=repeat, active=len(cpus),
                                       idle=idle_clients, total_clients=count)
                           for worker, cpus in zip(workers, allocations)]
                setups = [future.result() for future in futures]
            ranks = []
            mapping = []
            for worker, cpus, setup in zip(workers, allocations, setups):
                if len(setup['clients']) != len(cpus):
                    raise RuntimeError(f'{worker.name}: aliases do not cover every allocated client')
                for cpu, client in zip(cpus, setup['clients']):
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
            engine.warm(ranks, args.files, args.file_size_bytes, diagnostics / 'warm',
                        csv_dir, read)
            parallel(workers, 'sample-start')
            phases, placements = engine.measure(ranks, args.files, args.file_size_bytes,
                                                diagnostics / 'measured', csv_dir, read)
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
            case = f'{topology}/N{count}' + (f'-M{idle_clients}' if idle_clients else '')
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
                rank_mean_s = sum(phase.rank_elapsed_s) / len(phase.rank_elapsed_s)
                metrics = {
                    'ops': phase.operations, 'elapsed_s': phase.elapsed_s,
                    'ops_per_s': rate, 'ops_per_s_per_client': rate / count,
                    'rank_mean_us_per_op': sum(phase.rank_elapsed_s) / phase.operations * 1e6,
                    # Mean share of the phase a rank spends waiting (spinning) in the
                    # phase-end barrier for the slowest rank.
                    'barrier_wait_fraction': 1 - rank_mean_s / phase.elapsed_s,
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
