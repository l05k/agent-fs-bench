"""Single entry point: run a workload on explicit targets, or plot finished runs.

`run` writes only raw observations: `results/<bench>/<run-id>/config.json` (parameter
snapshot) and one `<table>.csv` per observation kind. Summaries and figures come from
the separate `plot` command.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import signal
import sys
from typing import Any

from . import analysis, cluster
from .results import PROJECT_ROOT, RunContext, Tables, git_commit, source_digest, write_json
from .targets import Target, load_targets

BENCHMARKS = (
    'clone', 'dev-workflow', 'mini-dev-workflow', 'npm-install', 'npm-install-nofsync',
    'kvtest', 'sqlite-speedtest', 'mount-scale', 'mdtest', 'mdtest-peak', 'agent-replay',
)
RESULTS_DIR = PROJECT_ROOT / 'results'
TARGETS_FILE = PROJECT_ROOT / 'local' / 'targets.toml'
# The one random seed of every run: workloads that generate input content (clone's file
# bytes) draw it from `random.Random(args.seed)`; config.json records it as `config.seed`.
SEED = 20261110


def parse_run_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog='agent-fs-bench run',
        description='Run one workload sequentially on explicit target directories. '
                    'Put workload flags after --; use -- --help for workload help.')
    parser.add_argument('benchmark', choices=BENCHMARKS)
    parser.add_argument('--dirs', nargs='+', metavar='DIRECTORY')
    parser.add_argument('--target', action='append', help='Select a registered alias; repeatable.')
    parser.add_argument('--mode', choices=cluster.MODES, default='local',
                        help='local: this machine only; overflow: this machine first, then '
                             'local/hosts.toml in order; remote: local/hosts.toml only')
    split = argv.index('--') if '--' in argv else len(argv)
    return parser.parse_args(argv[:split]), argv[split + 1:]


def select_targets(options: argparse.Namespace, config: dict[str, Any]) -> list[Target]:
    targets = load_targets(TARGETS_FILE, options.dirs)
    names = {target.name for target in targets}
    copiers = set(config.get('target_copiers', {}))
    assert copiers <= names, f'target_copiers aliases: expected within {names}, measured {copiers}'
    if options.target:
        assert set(options.target) <= names, \
            f'targets: expected within {names}, measured {options.target}'
        targets = [target for target in targets if target.name in options.target]
    if options.mode != 'remote':
        # Remote machines check their own target; the controller only where it runs tasks.
        for target in targets:
            assert target.path.is_dir(), f'target {target.name}: expected a mounted directory, ' \
                                         f'measured {target.path}'
            assert not RESULTS_DIR.is_relative_to(target.path.resolve()), \
                f'results: expected outside {target.path}, measured {RESULTS_DIR}'
    return targets


def workload_args(module, workload_argv: list[str]) -> argparse.Namespace:
    """The workload's parameters plus the run seed, parsed once per target."""
    return argparse.Namespace(**vars(module.parse_args(workload_argv)), seed=SEED)


def snapshot(benchmark: str, run_id: str, config: dict[str, Any], pool: cluster.Pool,
             tables: Tables, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Parameters (incl. seed) and environment baseline of one run (config.json)."""
    return {'benchmark': benchmark, 'run_id': run_id,
            'git_commit': git_commit(), 'source_sha256': source_digest(), 'config': config,
            'mpi_version': pool.mpi_version, 'nodes': pool.manifest(),
            'targets': targets, 'tables': tables.schemas}


def run_target(module, workload_argv: list[str], benchmark: str, run_id: str, run_dir: Path,
               target: Target, tables: Tables, pool: cluster.Pool) -> dict[str, Any]:
    print(f'[{benchmark}] {target.name}: {target.path}', flush=True)
    result_dir = run_dir / target.name
    result_dir.mkdir()
    context = RunContext(benchmark, run_id, target,
                         target.path / 'agent-fs-bench' / benchmark / run_id, result_dir,
                         tables, pool=pool)
    module.run(context, workload_args(module, workload_argv))
    print(f'  {context.rows} rows', flush=True)
    return {'name': target.name, 'path': str(target.path), 'info': context.info}


def run_benchmark(argv: list[str]) -> int:
    options, workload_argv = parse_run_args(argv)
    module = analysis.workload(options.benchmark)
    launcher = cluster.Launcher(**cluster.CONFIG)
    config = {**vars(workload_args(module, workload_argv)), 'mode': options.mode,
              'launcher': launcher.record()}
    targets = select_targets(options, config)
    pool = cluster.open_pool(options.mode, launcher)
    cluster.validate_pool(pool, source_digest())
    print(f'[{options.benchmark}] mode {options.mode}: ' + ', '.join(
        f'{node.name} ({node.hostname}, {node.facts["cpu_count"]} CPUs)' for node in pool.nodes),
        flush=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run_dir = RESULTS_DIR / options.benchmark / run_id
    run_dir.mkdir(parents=True)
    tables, finished = Tables(run_dir), []
    write_json(run_dir / 'config.json',
               snapshot(options.benchmark, run_id, config, pool, tables, finished))
    for target in targets:
        finished.append(run_target(module, workload_argv, options.benchmark, run_id, run_dir,
                                   target, tables, pool))
        write_json(run_dir / 'config.json',
                   snapshot(options.benchmark, run_id, config, pool, tables, finished))
    tables.close()
    print(f'Results: {run_dir}', flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    def terminate(signum, frame):
        raise KeyboardInterrupt

    # SIGTERM unwinds like Ctrl-C so workloads still clean up.
    signal.signal(signal.SIGTERM, terminate)
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='agent-fs-bench')
    parser.add_argument('command', choices=('run', 'plot'))
    if not argv or argv[0] in ('-h', '--help'):
        parser.print_help()
        return 0
    command = parser.parse_args(argv[:1]).command
    if command == 'run':
        return run_benchmark(argv[1:])
    return analysis.main(argv[1:])
