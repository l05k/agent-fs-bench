"""Single-host weak scaling of independent agent mounts using upstream mdtest."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import time

from agent_fs_bench.results import PROJECT_ROOT, RunContext
from .mount_scale_mdtest import Mdtest
from .mount_scale_mounts import Client, Layout, load_profile
from . import mount_scale_resources as resources

CONFIG = {
    'profiles': PROJECT_ROOT / 'local' / 'mounts.toml',
    'clients': (1, 2, 4, 8, 16),
    'directories': 3,  # Total per agent: one private workspace + X-1 shared resources.
    'files': 1000,
    'file_size_bytes': 4096,
    'repeats': 3,
    'mdtest': 'mdtest',
    'mpi_launcher': ('mpiexec',),  # Local MPMD launcher; never pass a host list.
    'command_timeout_s': 300,
    'idle_sample_s': 1.0,
    'resource_sample_s': 0.1,
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory-smoke', action='store_true',
                        help='Ordinary directories only; N=1,2, 32 files, one repeat; '
                             'NEVER execute mount/unmount or read mount profiles.')
    options = parser.parse_args(argv)
    values = dict(CONFIG)
    if options.directory_smoke:
        values.update(clients=(1, 2), files=32, repeats=1, idle_sample_s=0.1)
    return argparse.Namespace(**values, directory_smoke=options.directory_smoke)


def _validate(args: argparse.Namespace) -> None:
    if platform.system() != 'Linux':
        raise ValueError('mount-scale requires Linux (/proc and a local MPI launcher)')
    for name in ('directories', 'files', 'file_size_bytes', 'repeats'):
        if type(getattr(args, name)) is not int or getattr(args, name) < 1:
            raise ValueError(f'CONFIG[{name!r}] must be a positive integer')
    counts = tuple(args.clients)
    if (not counts or counts[0] != 1
            or any(type(count) is not int or count < 1 for count in counts)
            or tuple(sorted(set(counts))) != counts):
        raise ValueError('clients must be increasing unique positive counts starting at 1')
    for name in ('idle_sample_s', 'resource_sample_s', 'command_timeout_s'):
        if getattr(args, name) <= 0:
            raise ValueError(f'CONFIG[{name!r}] must be positive')


def _idle(seconds: float) -> dict[str, int | float]:
    before = resources.snapshot()
    time.sleep(seconds)
    after = resources.snapshot()
    return {**resources.levels(after), **resources.interval(before, after)}


def _directory_clients(root: Path, shared: tuple[Path, ...], count: int) -> tuple[Client, ...]:
    clients = []
    for index in range(count):
        workspace = root / f'agent-{index:04d}' / 'workspace'
        workspace.mkdir(parents=True)
        clients.append(Client(workspace, shared))
    return tuple(clients)


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    _validate(args)
    # The common runner creates work_dir on local staging storage. Formal mounts
    # must NOT be nested in the volume used by admin_root.
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
    data_root.mkdir(parents=True, exist_ok=False)
    shared = tuple(data_root / f'shared-{number}' for number in range(1, args.directories))
    for path in shared:
        path.mkdir()
    engine = Mdtest(args.mdtest, args.mpi_launcher, args.command_timeout_s)
    engine.prepare(shared, args.files, args.file_size_bytes, ctx.result_dir / 'prepare')
    ctx.parameters = {
        'topology': topology,
        'directories_per_agent': args.directories,
        'client_counts': list(args.clients),
        'files_per_directory_per_agent': args.files,
        'file_size_bytes': args.file_size_bytes,
        'repeats': args.repeats,
        'cache': 'shared-preread-every-client; private-create-then-read',
        'fsync': False,
        'idle_sample_s': args.idle_sample_s,
        'resource_sample_s': args.resource_sample_s,
        'kernel': platform.release(),
        'cpu_count': os.cpu_count(),
    }
    if profile is not None:
        ctx.parameters['mount_profile_sha256'] = hashlib.sha256(args.profiles.read_bytes()).hexdigest()
    try:
        for repeat in range(1, args.repeats + 1):
            baseline_rates = {}
            for count in args.clients:
                name = f'N{count}-R{repeat}'
                print(f'  {topology} {name}, X={args.directories}', flush=True)
                local = ctx.work_dir / name
                local.mkdir()
                private = data_root / name
                private.mkdir()
                diagnostics = ctx.result_dir / name
                diagnostics.mkdir()
                layout = None
                if profile is None:
                    clients = _directory_clients(private, shared, count)
                else:
                    layout = Layout(profile, local, private, shared, count)
                    clients = layout.clients
                # Directory/fixture construction and sentinel work are outside
                # lifecycle command timers; steady-state always has warm reads.
                baseline = _idle(args.idle_sample_s)
                mount_metrics = None if layout is None else layout.mount()
                mounted = _idle(args.idle_sample_s)
                engine.warm([client.shared for client in clients], args.files,
                            args.file_size_bytes, diagnostics / 'warm')
                with resources.Sampler(args.resource_sample_s) as sampler:
                    phases = engine.measure(
                        [client.workspace for client in clients],
                        [client.shared for client in clients], args.files,
                        args.file_size_bytes, diagnostics / 'measured')
                unmount_metrics = None if layout is None else layout.unmount()
                released = _idle(args.idle_sample_s)
                ctx.parameters.update(engine.metadata, clients=count)
                case = f'{topology}/N{count}'
                if layout is not None:
                    ctx.emit(case + '/mount', mount_metrics, repeat=repeat,
                             details={'agent_elapsed_s': layout.mount_agent_elapsed_s})
                    ctx.emit(case + '/unmount', unmount_metrics, repeat=repeat,
                             details={'agent_elapsed_s': layout.unmount_agent_elapsed_s})
                ctx.emit(case + '/resources/baseline', baseline, repeat=repeat)
                for label, sample in (('mounted-idle', mounted), ('released-idle', released)):
                    metrics = dict(sample)
                    for key in ('used_bytes', 'slab_bytes', 'slab_unreclaimable_bytes', 'mount_entries'):
                        metrics[key + '_delta'] = sample[key] - baseline[key]
                    ctx.emit(case + '/resources/' + label, metrics, repeat=repeat)
                ctx.emit(case + '/resources/workload', sampler.metrics(), repeat=repeat)
                for phase in phases:
                    rate = phase.operations / phase.elapsed_s
                    if count == 1:
                        baseline_rates[phase.name] = rate
                    metrics = {
                        'ops': phase.operations,
                        'elapsed_s': phase.elapsed_s,
                        'ops_per_s': rate,
                        'scaling_efficiency': rate / (count * baseline_rates[phase.name]),
                        # Rank averages, NOT sampled operation latency or p99.
                        'rank_mean_us_per_op': sum(phase.rank_elapsed_s) / phase.operations * 1e6,
                    }
                    if phase.name.endswith(('/create', '/read')):
                        metrics['bytes'] = phase.operations * args.file_size_bytes
                        metrics['mib_per_s'] = metrics['bytes'] / phase.elapsed_s / (1024 * 1024)
                    ctx.emit(case + '/' + phase.name, metrics, repeat=repeat,
                             details={'rank_elapsed_s': phase.rank_elapsed_s})
                # Only remove our successfully completed, unmounted run data.
                shutil.rmtree(private)
                shutil.rmtree(local)
    except BaseException:
        print(f'mount-scale failed; inspect client mounts under {ctx.work_dir} '
              f'and data under {data_root}; no automatic unmount or retry', flush=True)
        raise
    shutil.rmtree(data_root)
