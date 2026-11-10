"""Paper figures from the raw per-observation CSV tables (the `plot` command).

Figures draw every raw observation: `line` shows the median and IQR of all pooled rows at
each numeric x, `box` one box per x category. No aggregated table is written.
"""

import argparse
from dataclasses import dataclass, field
import importlib
import json
from pathlib import Path
import re

from . import figure
from .results import read_table


@dataclass
class Figure:
    """One figure, or one per distinct `split` value, drawn from `<table>.csv`.

    `line` draws the median and IQR of all pooled rows at each numeric x;
    `box` draws one box per x category. Series are joined `series` columns,
    ordered by target, so the primary (first) target leads the legend.
    """
    table: str
    x: str
    y: str
    kind: str = 'line'
    series: tuple[str, ...] = ('target',)
    split: tuple[str, ...] = ()
    where: dict = field(default_factory=dict)
    xlabel: str | None = None
    ylabel: str | None = None
    xscale: float = 1.0  # divides raw values, e.g. 1024 * 1024 for MiB
    yscale: float = 1.0
    logx: bool = False
    logy: bool = False
    name: str | None = None  # file stem; default <table>-<y>


def workload(benchmark: str):
    return importlib.import_module(f'agent_fs_bench.benchmarks.{benchmark.replace("-", "_")}')


def _cell(value) -> str:
    """Render a `where` value the way Tables writes it."""
    return str(int(value) if isinstance(value, bool) else value)


def figure_data(spec: Figure, rows: list[dict], legend: list[str],
                size: tuple[float, float]) -> list[tuple[str, dict]]:
    """Group rows into one dataset per split value: series × x → raw y samples.

    A row joins the first legend entry equal to its series label (columns of
    `series` joined by '/') or to its target; rows matching no entry are dropped.
    """
    groups: dict[tuple, dict] = {}
    for row in rows:
        label = '/'.join(row[key] for key in spec.series)
        rank = next((index for index, name in enumerate(legend)
                     if name in (label, row['target'])), None)
        if rank is None or row[spec.y] == '' or any(
                row[key] != _cell(value) for key, value in spec.where.items()):
            continue
        group = groups.setdefault(tuple(row[key] for key in spec.split), {'x': {}, 'series': {}})
        x = float(row[spec.x]) / spec.xscale if spec.kind == 'line' else row[spec.x]
        group['x'].setdefault(x, None)
        _, points = group['series'].setdefault(label, (rank, {}))
        points.setdefault(x, []).append(float(row[spec.y]) / spec.yscale)
    datasets = []
    for split, group in groups.items():
        # Stable sort: legend order first, then first appearance within an entry.
        names = sorted(group['series'], key=lambda name: group['series'][name][0])
        xs = sorted(group['x']) if spec.kind == 'line' else list(group['x'])
        datasets.append((''.join('-' + re.sub(r'[^A-Za-z0-9.]+', '_', value) for value in split), {
            'kind': spec.kind, 'series': names, 'x': xs,
            'values': {name: [group['series'][name][1].get(x, []) for x in xs] for name in names},
            'xlabel': spec.xlabel or spec.x, 'ylabel': spec.ylabel or spec.y,
            'title': ', '.join(split), 'logx': spec.logx, 'logy': spec.logy, 'size': list(size),
        }))
    return datasets


def load_snapshots(run_dirs: list[Path], vary: tuple[str, ...]) -> list[dict]:
    """config.json of each run; pooled runs share benchmark, source and config except `vary`."""
    snapshots = [json.loads((run / 'config.json').read_text(encoding='utf-8')) for run in run_dirs]
    first = snapshots[0]
    for run, snapshot in zip(run_dirs[1:], snapshots[1:]):
        assert snapshot['benchmark'] == first['benchmark'], \
            f'{run} benchmark: expected {first["benchmark"]}, measured {snapshot["benchmark"]}'
        assert snapshot['source_sha256'] == first['source_sha256'], \
            f'{run} source_sha256: expected {first["source_sha256"]}, measured ' \
            f'{snapshot["source_sha256"]}; runs of different sources never pool'
        differ = sorted(key for key in first['config'].keys() | snapshot['config'].keys()
                        if key not in vary
                        and first['config'].get(key) != snapshot['config'].get(key))
        assert not differ, f'{run} config: expected equal to {run_dirs[0]} outside --vary, ' \
                           f'measured differences in {differ}'
    return snapshots


def plot(run_dirs: list[Path], figures: list[Figure] | None = None, *,
         targets: list[str] | None = None, output: Path | None = None,
         size: tuple[float, float] = (3.5, 1.5), vary: tuple[str, ...] = ()) -> list[Path]:
    """Render `figures` (default: the workload's FIGURES) and return the written stems.

    `targets` orders and filters the legend by target name or full series label
    (e.g. `LakeBase/cp-r`); the first is the primary system.
    """
    snapshots = load_snapshots(run_dirs, vary)
    finished = list(dict.fromkeys(
        target['name'] for snapshot in snapshots for target in snapshot['targets']))
    required = figures is not None
    figures = figures if required else getattr(workload(snapshots[0]['benchmark']), 'FIGURES', [])
    tables = {spec.table: [row for run in run_dirs if (run / f'{spec.table}.csv').exists()
                           for row in read_table(run / f'{spec.table}.csv')]
              for spec in figures}
    if targets:
        known = set(finished) | {'/'.join(row[key] for key in spec.series)
                                 for spec in figures for row in tables[spec.table]}
        assert set(targets) <= known, f'--targets: expected within {sorted(known)}, ' \
                                      f'measured {targets}'
    output = output or run_dirs[0] / 'figures'
    stems = []
    for spec in figures:
        datasets = figure_data(spec, tables[spec.table], targets or finished, size)
        assert datasets or not required, f'{spec.table}.csv rows matching {spec}: expected ' \
                                         f'>= 1, measured 0'
        for suffix, data in datasets:
            output.mkdir(parents=True, exist_ok=True)
            stem = output / ((spec.name or f'{spec.table}-{spec.y}') + suffix)
            write_script(stem, data)
            figure.render(data, stem)
            stems.append(stem)
    return stems


def write_script(stem: Path, data: dict) -> None:
    """Export the shared style plus this figure's raw data as a standalone script."""
    stem.with_name(stem.name + '.py').write_text(
        Path(figure.__file__).read_text(encoding='utf-8')
        + f'\n\nDATA = {data!r}\n\nif __name__ == "__main__":\n'
        + '    render(DATA, Path(__file__).with_suffix(""))\n',
        encoding='utf-8')


def plot_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='agent-fs-bench plot',
        description='Render the workload default figures, or one custom figure with --table.')
    parser.add_argument('runs', nargs='+', type=Path, help='run directories of one benchmark')
    parser.add_argument('--targets', type=lambda text: text.split(','),
                        help='comma-separated legend order and filter: target names or full '
                             'series labels (e.g. LakeBase/cp-r); the first is the primary system')
    parser.add_argument('--output', type=Path, help='figure directory (default: <first run>/figures)')
    parser.add_argument('--size', type=float, nargs=2, default=(3.5, 1.5),
                        metavar=('WIDTH', 'HEIGHT'), help='inches per figure')
    parser.add_argument('--vary', type=lambda text: tuple(text.split(',')), default=(),
                        help='comma-separated config keys allowed to differ between pooled runs')
    custom = parser.add_argument_group('custom figure')
    custom.add_argument('--table', help='CSV table name, e.g. phases')
    custom.add_argument('--x')
    custom.add_argument('--y')
    custom.add_argument('--kind', choices=('line', 'box'), default='line')
    custom.add_argument('--series', type=lambda text: tuple(text.split(',')), default=('target',))
    custom.add_argument('--split', type=lambda text: tuple(text.split(',')), default=())
    custom.add_argument('--where', action='append', default=[], metavar='COLUMN=VALUE')
    custom.add_argument('--xlabel')
    custom.add_argument('--ylabel')
    custom.add_argument('--xscale', type=float, default=1.0)
    custom.add_argument('--yscale', type=float, default=1.0)
    custom.add_argument('--logx', action='store_true')
    custom.add_argument('--logy', action='store_true')
    custom.add_argument('--name', help='file stem (default: <table>-<y>)')
    return parser


def main(argv: list[str]) -> int:
    parser = plot_parser()
    args = parser.parse_args(argv)
    figures = None
    if args.table:
        if not (args.x and args.y):
            parser.error('--table needs --x and --y')
        figures = [Figure(args.table, args.x, args.y, args.kind, args.series, args.split,
                          dict(item.split('=', 1) for item in args.where), args.xlabel,
                          args.ylabel, args.xscale, args.yscale, args.logx, args.logy, args.name)]
    stems = plot(args.runs, figures, targets=args.targets, output=args.output,
                 size=tuple(args.size), vary=args.vary)
    assert stems, f'figures from {args.runs}: expected >= 1, measured 0'
    for stem in stems:
        print(f'{stem}.pdf')
    return 0
