"""Single entry point: run a workload on explicit targets, or re-plot finished runs."""

import argparse
from datetime import datetime, timezone
import signal
import sys

from . import analysis, cluster
from .results import PROJECT_ROOT, RunContext, Tables, source_digest, write_json
from .targets import load_targets

BENCHMARKS = (
    'clone', 'dev-workflow', 'mini-dev-workflow', 'npm-install', 'npm-install-nofsync',
    'kvtest', 'sqlite-speedtest', 'mount-scale', 'mdtest', 'mdtest-peak', 'agent-replay',
)
RESULTS_DIR = PROJECT_ROOT / 'results'
TARGETS_FILE = PROJECT_ROOT / 'local' / 'targets.toml'


def run_benchmark(argv: list[str]) -> int:
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
    options = parser.parse_args(argv[:split])
    workload_argv = argv[split + 1:]
    module = analysis.workload(options.benchmark)
    # Mode and launcher settings are part of the experiment: plot pools runs of one mode
    # and one launcher configuration unless --vary names them.
    config = {**vars(module.parse_args(workload_argv)), 'mode': options.mode,
              'launcher_sha256': cluster.launcher_digest()}
    targets = load_targets(TARGETS_FILE, options.dirs)
    # A misspelled override would silently measure cp-r; check before --target filtering.
    unknown = set(config.get('target_copiers', {})) - {target.name for target in targets}
    if unknown:
        raise ValueError(f'unknown target_copiers aliases: {", ".join(sorted(unknown))}; '
                         f'targets: {", ".join(target.name for target in targets)}')
    if options.target:
        unknown = set(options.target) - {target.name for target in targets}
        if unknown:
            raise ValueError(f'unknown targets: {", ".join(sorted(unknown))}')
        targets = [target for target in targets if target.name in options.target]
    if options.mode != 'remote':
        # Remote machines check their own target; the controller only where it runs tasks.
        for target in targets:
            if not target.path.is_dir():
                raise ValueError(f'target directory does not exist: {target.path}; mount it first')
            if RESULTS_DIR.is_relative_to(target.path.resolve()):
                raise ValueError('results must be outside every measured target directory')
    pool = cluster.open_pool(options.mode)
    print(f'[{options.benchmark}] mode {options.mode}: ' + ', '.join(
        f'{node.name} ({node.hostname}, {len(node.cpus)} CPUs)' for node in pool.nodes), flush=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run_dir = RESULTS_DIR / options.benchmark / run_id
    run_dir.mkdir(parents=True)
    tables = Tables(run_dir)
    manifest = {'benchmark': options.benchmark, 'run_id': run_id,
                'source_sha256': source_digest(), 'config': config,
                'launcher': pool.launcher, 'nodes': pool.manifest(),
                'targets': [], 'tables': tables.schemas}
    write_json(run_dir / 'manifest.json', manifest)
    try:
        for target in targets:
            # Each participating machine creates its own <work_dir>/<node>/ when it starts.
            work_dir = target.path / 'agent-fs-bench' / options.benchmark / run_id
            result_dir = run_dir / target.name
            result_dir.mkdir()
            context = RunContext(options.benchmark, run_id, target, work_dir, result_dir, tables,
                                 pool=pool)
            print(f'[{options.benchmark}] {target.name}: {target.path}', flush=True)
            # A fresh Namespace per target keeps workload setup from leaking state.
            module.run(context, module.parse_args(workload_argv))
            print(f'  {context.rows} rows', flush=True)
            manifest['targets'].append({'name': target.name, 'path': str(target.path),
                                        'info': context.info})
            write_json(run_dir / 'manifest.json', manifest)
    finally:
        tables.close()
    analysis.summarize(run_dir)
    for stem in analysis.plot([run_dir]):
        print(f'Figure: {stem}.pdf')
    print(f'Results: {run_dir}', flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    def terminate(signum, frame):
        raise KeyboardInterrupt

    # SIGTERM unwinds like Ctrl-C so workloads still unmount and clean up.
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
