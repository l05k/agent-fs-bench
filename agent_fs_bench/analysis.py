"""Offline DuckDB queries and plotting over portable JSONL measurements."""

import argparse
import csv
import glob
import json
from pathlib import Path
import sys
from typing import TextIO

COLUMNS = {
    'benchmark': 'VARCHAR', 'run_id': 'VARCHAR',
    'target': 'JSON', 'case': 'VARCHAR', 'repeat': 'BIGINT',
    'parameters': 'JSON', 'source_sha256': 'VARCHAR',
    'metrics': 'JSON', 'details': 'JSON',
}
METRICS_SQL = '''
SELECT benchmark, run_id, target->>'name' AS target,
       target->>'path' AS target_path, "case", repeat, parameters, source_sha256,
       details, item.key AS metric, CAST(item.value AS DOUBLE) AS value
FROM measurements, json_each(metrics) AS item
'''
SUMMARY_SQL = '''
WITH series AS (
    SELECT benchmark, run_id, "case", metric, value,
           CASE WHEN benchmark = 'clone'
                THEN target || '/' || (details->>'copier')
                ELSE target END AS target
    FROM metrics
)
SELECT target, "case", metric, count(*) AS samples, avg(value) AS mean,
       median(value) AS median, min(value) AS minimum, max(value) AS maximum,
       stddev_samp(value) AS stddev, benchmark,
       string_agg(DISTINCT run_id, ';' ORDER BY run_id) AS run_ids
FROM series
GROUP BY benchmark, target, "case", metric
ORDER BY benchmark, "case", metric, target
'''

MIB = 1024 * 1024
# Per-request samples are pooled; other metrics are one batch value per repeat.
CLONE_RAW_METRICS = frozenset({'latency_s', 'copy_s', 'fsync_s'})
# Each sweep varies only its numeric x dimension; the workload payload stays fixed.
CLONE_GROUPS = (
    {'suffix': 'bytes', 'experiment': 'bytes', 'kind': None,
     'x_metric': 'total_bytes', 'x_scale': MIB, 'xlabel': 'Total bytes (MiB)'},
    {'suffix': 'files', 'experiment': 'files', 'kind': None,
     'x_metric': 'total_files', 'x_scale': 1, 'xlabel': 'Total files'},
    {'suffix': 'concurrency-large', 'experiment': 'concurrency', 'kind': 'large',
     'x_metric': 'concurrency', 'x_scale': 1, 'xlabel': 'Concurrency'},
    {'suffix': 'concurrency-small', 'experiment': 'concurrency', 'kind': 'small',
     'x_metric': 'concurrency', 'x_scale': 1, 'xlabel': 'Concurrency'},
)
CLONE_LOG_X = frozenset({'bytes', 'files'})
CLONE_YLABELS = {
    'latency_s': 'Clone latency (s)',
    'copy_s': 'Copy / clone time (s)',
    'fsync_s': 'Sync time (s)',
    'clones_s': 'Throughput (clones/s)',
    'wall_s': 'Batch time (s)',
}


def connect(inputs: list[str], database: Path | None = None):
    import duckdb

    files = sorted({str(Path(path).resolve()) for pattern in inputs
                    for path in glob.glob(pattern, recursive=True)})
    if not files:
        raise ValueError('no JSONL files match the input paths')
    connection = duckdb.connect(str(database) if database else ':memory:')
    connection.read_json(files, format='newline_delimited', columns=COLUMNS,
                         maximum_object_size=134217728).create_view('input_records')
    if database:
        connection.execute('CREATE OR REPLACE TABLE measurements AS SELECT * FROM input_records')
        connection.execute('DROP VIEW input_records')
    else:
        connection.execute('CREATE VIEW measurements AS SELECT * FROM input_records')
    connection.execute('CREATE OR REPLACE VIEW metrics AS ' + METRICS_SQL)
    return connection


def write_csv(cursor, handle: TextIO) -> int:
    """Write query columns and rows using the same CSV format in every entry point."""
    writer = csv.writer(handle)
    writer.writerow([column[0] for column in cursor.description])
    rows = cursor.fetchall()
    writer.writerows(rows)
    return len(rows)


def export_summary(jsonl: Path, output: Path) -> int:
    """Write the standard target/case/metric summary; return its row count."""
    connection = connect([str(jsonl)])
    try:
        cursor = connection.execute(SUMMARY_SQL)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('w', newline='', encoding='utf-8') as handle:
            return write_csv(cursor, handle)
    finally:
        connection.close()


def main(command: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f'agent-fs-bench {command}')
    parser.add_argument('inputs', nargs='+', help='JSONL files or quoted glob patterns')
    if command == 'analyze':
        parser.add_argument('--sql', default=SUMMARY_SQL,
                            help='SQL over measurements (nested JSON) or metrics (numeric rows)')
        parser.add_argument('--database', type=Path, help='Rebuild a local DuckDB analysis database')
        parser.add_argument('--output', type=Path, help='Write query CSV instead of stdout')
    else:
        parser.add_argument('--benchmark', required=True)
        parser.add_argument('--metric', required=True, help='Metric name, including its unit')
        parser.add_argument('--cases', type=lambda text: text.split(','),
                            help='Comma-separated x-axis order; also selects cases (default: all, sorted)')
        parser.add_argument('--targets', type=lambda text: text.split(','),
                            help='Comma-separated legend order; the first is the primary system')
        parser.add_argument('--ylabel', help='Y-axis label (default: metric name)')
        parser.add_argument('--size', type=float, nargs=2, default=(3.5, 1.5),
                            metavar=('WIDTH', 'HEIGHT'), help='Figure size in inches')
        parser.add_argument('--output', type=Path, required=True,
                            help='Output stem; writes <stem>.pdf, <stem>.png and self-contained <stem>.py')
    args = parser.parse_args(argv)
    connection = connect(args.inputs, getattr(args, 'database', None))
    try:
        if command == 'plot':
            plot(connection, args)
        else:
            cursor = connection.execute(args.sql)
            handle = args.output.open('w', newline='', encoding='utf-8') if args.output else sys.stdout
            try:
                write_csv(cursor, handle)
            finally:
                if args.output:
                    handle.close()
    finally:
        connection.close()
    return 0


def plot(connection, args: argparse.Namespace) -> None:
    from . import figure

    if args.benchmark == 'clone':
        plot_clone(connection, args)
        return

    rows = connection.execute('''
        SELECT target, "case", list(value ORDER BY run_id, repeat)
        FROM metrics WHERE benchmark = ? AND metric = ?
        GROUP BY target, "case" ORDER BY "case", target
    ''', [args.benchmark, args.metric]).fetchall()
    if not rows:
        raise ValueError('no measurements match the plot selection')
    found_targets = sorted({target for target, _, _ in rows})
    found_cases = sorted({case for _, case, _ in rows})
    targets = args.targets or found_targets
    cases = args.cases or found_cases
    for kind, wanted, found in (('targets', targets, found_targets), ('cases', cases, found_cases)):
        missing = [name for name in wanted if name not in found]
        if missing:
            raise ValueError(f'no measurements for {kind}: {", ".join(missing)}')
    samples = {target: {} for target in targets}
    for target, case, group in rows:
        if target in samples and case in cases:
            samples[target][case] = group
    data = {
        'cases': cases, 'targets': targets, 'samples': samples,
        'ylabel': args.ylabel or args.metric, 'size': list(args.size),
    }
    stem = args.output
    stem.parent.mkdir(parents=True, exist_ok=True)
    write_script(stem, data, 'render')
    figure.render(data, stem)


def write_script(stem: Path, data: dict, renderer: str) -> None:
    """Export the shared style plus raw data as a standalone figure script."""
    from . import figure

    script = stem.with_name(stem.name + '.py')
    script.write_text(
        Path(figure.__file__).read_text(encoding='utf-8')
        + f'\n\nDATA = {data!r}\n\nif __name__ == "__main__":\n'
        + f'    {renderer}(DATA, Path(__file__).with_suffix(""))\n',
        encoding='utf-8')


def plot_clone(connection, args: argparse.Namespace) -> None:
    from . import figure

    figures = clone_plot_data(connection, args)
    if not figures:
        raise ValueError('no clone measurements match the plot selection')
    stem = args.output
    stem.parent.mkdir(parents=True, exist_ok=True)
    for suffix, data in figures:
        output = stem.with_name(f'{stem.name}-{suffix}')
        write_script(output, data, 'render_line')
        figure.render_line(data, output)


def clone_plot_data(connection, args: argparse.Namespace) -> list[tuple[str, dict]]:
    """Build one line-figure dataset per clone sweep present in the selection."""
    rows = connection.execute('''
        SELECT target->>'name', "case", metrics::VARCHAR, details::VARCHAR
        FROM measurements WHERE benchmark = 'clone'
        ORDER BY run_id, repeat, target->>'name', "case"
    ''').fetchall()
    records = []
    for target, case, metrics_text, details_text in rows:
        metrics = json.loads(metrics_text)
        if args.metric not in metrics:
            continue
        details = json.loads(details_text)
        details['series'] = f"{target}/{details['copier']}"
        records.append((case, metrics, details))
    if not records:
        raise ValueError('no measurements match the plot selection')

    found_cases = sorted({case for case, _, _ in records})
    found_series = sorted({details['series'] for _, _, details in records})
    series = args.targets or found_series
    cases = args.cases or found_cases
    for kind, wanted, found in (('targets', series, found_series), ('cases', cases, found_cases)):
        missing = [name for name in wanted if name not in found]
        if missing:
            raise ValueError(f'no measurements for {kind}: {", ".join(missing)}')
    selected = [(metrics, details) for case, metrics, details in records
                if case in cases and details['series'] in series]

    figures = []
    for group in CLONE_GROUPS:
        rows = [record for record in selected
                if record[1]['experiment'] == group['experiment']
                and (group['kind'] is None or record[1]['kind'] == group['kind'])]
        if not rows:
            continue
        figures.append((group['suffix'], clone_line_data(group, rows, series, args)))
    return figures


def clone_line_data(group: dict, rows: list, series: list[str],
                    args: argparse.Namespace) -> dict:
    """Validate sweep comparability and pool raw request samples per series and x."""
    payloads = [(metrics['total_bytes'], metrics['total_files']) for metrics, _ in rows]
    if group['experiment'] == 'bytes':
        comparability = {files for _, files in payloads}
        description = 'total file counts'
    elif group['experiment'] == 'files':
        comparability = {total_bytes / files for total_bytes, files in payloads}
        description = 'file sizes'
    else:
        comparability = set(payloads)
        description = 'payload sizes'
    if len(comparability) != 1:
        raise ValueError(f'clone {group["suffix"]} sweep mixes incompatible '
                         f'{description}: {len(comparability)} variants')

    present = {details['series'] for _, details in rows}
    missing = [name for name in series if name not in present]
    if missing:
        raise ValueError(f'no clone {group["suffix"]} measurements for targets: '
                         f'{", ".join(missing)}')

    points = {name: {} for name in series}
    for metrics, details in rows:
        x = metrics[group['x_metric']] / group['x_scale']
        if args.metric in CLONE_RAW_METRICS:
            requests = details['requests']
            if not all(args.metric in request for request in requests):
                raise ValueError(f'clone request samples do not contain {args.metric}')
            values = [request[args.metric] for request in requests]
        else:
            values = [metrics[args.metric]]
        points[details['series']].setdefault(x, []).extend(values)

    expected = set(points[series[0]])
    for name in series[1:]:
        if set(points[name]) != expected:
            raise ValueError(f'clone {group["suffix"]} series do not cover the same x values: '
                             f'{name} differs from {series[0]}')

    values = [value for name in series for group_values in points[name].values()
              for value in group_values]
    ordered = {name: [{'x': x, 'values': sorted(points[name][x])}
                      for x in sorted(points[name])] for name in series}
    data = {
        'series': series,
        'points': ordered,
        'xlabel': group['xlabel'],
        'ylabel': args.ylabel or CLONE_YLABELS.get(args.metric, args.metric),
        'logx': group['suffix'] in CLONE_LOG_X,
        'logy': args.metric in CLONE_RAW_METRICS and min(values) > 0,
        'size': list(args.size),
    }
    return data
