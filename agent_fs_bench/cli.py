"""Single entry point for explicit targets, workloads, and offline analysis."""

import argparse
from datetime import datetime, timezone
import importlib
import signal
import sys

from .results import PROJECT_ROOT, RunContext, source_digest
from .targets import load_targets

BENCHMARKS = {
    'clone': 'clone',
    'dev-workflow': 'dev_workflow',
    'mini-dev-workflow': 'mini_dev_workflow',
    'npm-install': 'npm_install',
    'kvtest': 'kvtest',
    'sqlite-speedtest': 'sqlite_speedtest',
    'multi-host': 'multi_host',
}
RESULTS_DIR = PROJECT_ROOT / 'results'
TARGETS_FILE = PROJECT_ROOT / 'local' / 'targets.toml'


def run_benchmark(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='agent-fs-bench run',
        description='Run one workload sequentially on explicit, pre-mounted directories. '
                    'Put workload flags after --; use -- --help for workload help.')
    parser.add_argument('benchmark', choices=BENCHMARKS)
    parser.add_argument('--dirs', nargs='+', metavar='DIRECTORY')
    parser.add_argument('--target', action='append', help='Select a registered alias; repeatable.')
    split = argv.index('--') if '--' in argv else len(argv)
    options = parser.parse_args(argv[:split])
    workload_argv = argv[split + 1:] if split < len(argv) else []
    module = importlib.import_module(f'agent_fs_bench.benchmarks.{BENCHMARKS[options.benchmark]}')
    workload_args = module.parse_args(workload_argv)
    targets = load_targets(TARGETS_FILE, options.dirs)
    target_names = {target.name for target in targets}
    if options.benchmark == 'clone':
        unknown = set(workload_args.target_copiers) - target_names
        if unknown:
            raise ValueError(
                f'unknown target_copiers aliases: {", ".join(sorted(unknown))}; '
                f'available targets: {", ".join(sorted(target_names))}'
            )
    if options.target:
        unknown = set(options.target) - target_names
        if unknown:
            raise ValueError(f'unknown targets: {", ".join(sorted(unknown))}')
        targets = [target for target in targets if target.name in options.target]
    remote = getattr(module, 'REMOTE', False)
    results_root = RESULTS_DIR
    if not remote:
        for target in targets:
            if not target.path.is_dir():
                raise ValueError(f'target directory does not exist: {target.path}; mount it first')
            if results_root.is_relative_to(target.path.resolve()):
                raise ValueError('results must be outside every measured target directory')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run_dir = results_root / options.benchmark / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    source_sha256 = source_digest()
    output_path = run_dir / 'results.jsonl'
    with output_path.open('x', encoding='utf-8') as output:
        for target in targets:
            work_dir = target.path / 'agent-fs-bench' / options.benchmark / run_id
            result_dir = run_dir / target.name
            result_dir.mkdir()
            if not remote:
                if not work_dir.resolve().is_relative_to(target.path.resolve()):
                    raise ValueError(f'work directory escapes target through a symlink: {work_dir}')
                work_dir.mkdir(parents=True, exist_ok=False)
            # A new Namespace per target prevents workload setup from leaking state.
            target_args = module.parse_args(workload_argv)
            context = RunContext(options.benchmark, run_id, target, work_dir,
                                 result_dir, vars(target_args).copy(), source_sha256, output)
            print(f'[{options.benchmark}] {target.name}: {target.path}', flush=True)
            module.run(context, target_args)
            print(f'  {context.records} measurements', flush=True)
    print(f'Results: {output_path}', flush=True)
    from . import analysis
    summary_path = run_dir / 'summary.csv'
    try:
        rows = analysis.export_summary(output_path, summary_path)
    except Exception as exc:
        print(f'error: cannot export CSV: {exc}', file=sys.stderr)
        return 1
    print(f'Summary: {summary_path} ({rows} rows)')
    return 0


def main(argv: list[str] | None = None) -> int:
    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='agent-fs-bench')
    parser.add_argument('command', choices=('run', 'analyze', 'plot'))
    if not argv or argv[0] in ('-h', '--help'):
        parser.print_help()
        return 0
    command = parser.parse_args(argv[:1]).command
    try:
        if command == 'run':
            return run_benchmark(argv[1:])
        from . import analysis
        return analysis.main(command, argv[1:])
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
