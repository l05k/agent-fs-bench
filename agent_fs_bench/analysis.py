"""Offline DuckDB queries and plotting over portable JSONL measurements."""

import argparse
import csv
import glob
import json
from pathlib import Path
import sys
from typing import TextIO

from .results import canonical_json

COLUMNS = {
    'benchmark': 'VARCHAR', 'run_id': 'VARCHAR',
    'target': 'JSON', 'case': 'VARCHAR', 'repeat': 'BIGINT',
    'parameters': 'JSON', 'source_sha256': 'VARCHAR',
    'metrics': 'JSON', 'details': 'JSON',
}
METRICS_SQL = '''
SELECT benchmark, run_id, target->>'name' AS target,
       target->>'path' AS target_path, "case", repeat, parameters,
       COALESCE(parameter_canonical.canonical, CAST(parameters AS VARCHAR)) AS parameters_canonical,
       source_sha256, details,
       item.key AS metric, CAST(item.value AS DOUBLE) AS value
FROM measurements
LEFT JOIN parameter_canonical
       ON CAST(measurements.parameters AS VARCHAR) = parameter_canonical.raw
CROSS JOIN json_each(measurements.metrics) AS item
'''
SUMMARY_SQL = '''
WITH series AS (
    SELECT benchmark, run_id, "case", metric, value,
           parameters_canonical, source_sha256,
           CASE WHEN benchmark = 'clone'
                THEN target || '/' || (details->>'copier')
                ELSE target END AS target
    FROM metrics
)
SELECT target, "case", metric, count(*) AS samples, avg(value) AS mean,
       median(value) AS median, min(value) AS minimum, max(value) AS maximum,
       stddev_samp(value) AS stddev, benchmark,
       string_agg(DISTINCT run_id, ';' ORDER BY run_id) AS run_ids,
       parameters_canonical AS parameters, source_sha256
FROM series
GROUP BY benchmark, target, "case", metric, parameters_canonical, source_sha256
ORDER BY benchmark, "case", metric, target, parameters_canonical, source_sha256
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

def canonical_parameter_text(value) -> str:
    """Canonicalize one JSON value so key order never changes the grouping key."""
    return canonical_json(json.loads(value))

def parameters_argument(text: str) -> str:
    try:
        return canonical_parameter_text(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            'expected JSON, for example \'{"fixture": "pnpm-bench", "concurrency": 4}\'') from None

MDTEST_OPERATIONS = {
    'file-create': 'File create', 'file-stat': 'File stat',
    'file-open': 'File open/close', 'file-unlink': 'File unlink',
    'dir-mkdir': 'Directory mkdir', 'dir-stat': 'Directory stat',
    'dir-rename': 'Directory rename', 'dir-rmdir': 'Directory rmdir',
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
    # DuckDB keeps JSON object key order, so canonicalize distinct parameter
    # documents in Python once and join them back; this needs no UDF extras.
    documents = connection.execute(
        'SELECT DISTINCT CAST(parameters AS VARCHAR) FROM input_records').fetchall()
    connection.execute('CREATE TEMP TABLE parameter_canonical (raw VARCHAR, canonical VARCHAR)')
    if documents:
        connection.executemany(
            'INSERT INTO parameter_canonical VALUES (?, ?)',
            [(raw, None if raw is None else canonical_parameter_text(raw))
             for (raw,) in documents])
    if database:
        connection.execute('CREATE OR REPLACE TABLE measurements AS SELECT * FROM input_records')
        connection.execute('DROP VIEW input_records')
        connection.execute('DROP TABLE IF EXISTS metrics')
        connection.execute('CREATE TABLE metrics AS ' + METRICS_SQL)
    else:
        connection.execute('CREATE VIEW measurements AS SELECT * FROM input_records')
        connection.execute('CREATE VIEW metrics AS ' + METRICS_SQL)
    return connection

def write_csv(cursor, handle: TextIO) -> int:
    """Write query columns and rows using the same CSV format in every entry point."""
    writer = csv.writer(handle)
    writer.writerow([column[0] for column in cursor.description])
    rows = cursor.fetchall()
    writer.writerows(rows)
    return len(rows)

def export_summary(jsonl: Path, output: Path) -> int:
    """Write the standard target/case/metric/identity summary; return its row count."""
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
        parser.add_argument('--parameters', type=parameters_argument,
                            help='Plot only rows whose parameters canonicalize to exactly this JSON')
        parser.add_argument('--source-sha256',
                            type=lambda text: [value.strip() for value in text.split(',')
                                               if value.strip()],
                            help='Comma-separated source fingerprints; plot only matching rows')
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

def filter_identities(rows: list, wanted_parameters: str | None,
                      wanted_sources: list[str] | None, identity) -> list:
    """Keep rows whose (parameters, source) identity matches the plot selectors."""
    if wanted_sources is not None and not wanted_sources:
        raise ValueError('--source-sha256 needs at least one fingerprint')
    kept = []
    for row in rows:
        parameters, source = identity(row)
        if wanted_parameters is not None and parameters != wanted_parameters:
            continue
        if wanted_sources and source not in wanted_sources:
            continue
        kept.append(row)
    return kept

def selection_detail(wanted_parameters: str | None, wanted_sources: list[str] | None) -> str:
    """Describe the active identity filters for a no-match error."""
    filters = []
    if wanted_parameters is not None:
        filters.append(f'parameters={wanted_parameters}')
    if wanted_sources:
        filters.append('source_sha256=' + ','.join(wanted_sources))
    return f' for {"; ".join(filters)}' if filters else ''

def describe_identities(identities: set[tuple[str, str]]) -> str:
    """List distinct identities so an ambiguity error names every candidate."""
    return '; '.join(f'parameters={parameters} source_sha256={source}'
                     for parameters, source in sorted(identities))

def plot(connection, args: argparse.Namespace) -> None:
    """Render selected measurements, refusing to merge configurations within a case."""
    from . import figure

    if args.benchmark == 'clone':
        plot_clone(connection, args)
        return
    if args.benchmark == 'mdtest':
        for suffix, data in mdtest_plot_data(connection, args):
            stem = args.output.with_name(f'{args.output.name}-{suffix}')
            stem.parent.mkdir(parents=True, exist_ok=True)
            write_script(stem, data, 'render_line')
            figure.render_line(data, stem)
        return

    rows = connection.execute('''
        SELECT target, "case", parameters_canonical, source_sha256,
               list(value ORDER BY run_id, repeat)
        FROM metrics WHERE benchmark = ? AND metric = ?
        GROUP BY target, "case", parameters_canonical, source_sha256
        ORDER BY "case", target, source_sha256, parameters_canonical
    ''', [args.benchmark, args.metric]).fetchall()
    wanted_parameters = getattr(args, 'parameters', None)
    wanted_sources = getattr(args, 'source_sha256', None)
    rows = filter_identities(rows, wanted_parameters, wanted_sources,
                             lambda row: (row[2], row[3]))
    if not rows:
        raise ValueError('no measurements match the plot selection'
                         + selection_detail(wanted_parameters, wanted_sources))
    found_targets = sorted({target for target, _, _, _, _ in rows})
    found_cases = sorted({case for _, case, _, _, _ in rows})
    targets = args.targets or found_targets
    cases = args.cases or found_cases
    for kind, wanted, found in (('targets', targets, found_targets), ('cases', cases, found_cases)):
        missing = [name for name in wanted if name not in found]
        if missing:
            raise ValueError(f'no measurements for {kind}: {", ".join(missing)}')
    # Validate the selected cases and targets before checking identity so rows
    # excluded by --targets/--cases can never trigger an ambiguity error.
    selected = [row for row in rows if row[0] in targets and row[1] in cases]
    identities: dict[str, set[tuple[str, str]]] = {}
    for _, case, parameters, source, _ in selected:
        identities.setdefault(case, set()).add((parameters, source))
    for case in cases:
        found = identities.get(case, set())
        if len(found) > 1:
            raise ValueError(
                f'case {case!r} mixes {len(found)} configurations: '
                f'{describe_identities(found)}; '
                'plot one with --parameters <json> and/or --source-sha256 <hash>[,<hash>]')
    samples = {target: {} for target in targets}
    for target, case, _, _, group in selected:
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
        SELECT target->>'name', "case",
               parameter_canonical.canonical, source_sha256,
               metrics::VARCHAR, details::VARCHAR
        FROM measurements
        LEFT JOIN parameter_canonical
               ON CAST(measurements.parameters AS VARCHAR) = parameter_canonical.raw
        WHERE benchmark = 'clone'
        ORDER BY run_id, repeat, target->>'name', "case"
    ''').fetchall()
    records = []
    for target, case, parameters, source, metrics_text, details_text in rows:
        metrics = json.loads(metrics_text)
        if args.metric not in metrics:
            continue
        details = json.loads(details_text)
        details['series'] = f"{target}/{details['copier']}"
        records.append((case, parameters, source, metrics, details))
    wanted_parameters = getattr(args, 'parameters', None)
    wanted_sources = getattr(args, 'source_sha256', None)
    records = filter_identities(records, wanted_parameters, wanted_sources,
                                lambda record: (record[1], record[2]))
    if not records:
        raise ValueError('no clone measurements match the plot selection'
                         + selection_detail(wanted_parameters, wanted_sources))

    found_cases = sorted({case for case, _, _, _, _ in records})
    found_series = sorted({details['series'] for _, _, _, _, details in records})
    series = args.targets or found_series
    cases = args.cases or found_cases
    for kind, wanted, found in (('targets', series, found_series), ('cases', cases, found_cases)):
        missing = [name for name in wanted if name not in found]
        if missing:
            raise ValueError(f'no measurements for {kind}: {", ".join(missing)}')
    selected = [record for record in records
                if record[0] in cases and record[4]['series'] in series]

    figures = []
    for group in CLONE_GROUPS:
        rows = [record for record in selected
                if record[4]['experiment'] == group['experiment']
                and (group['kind'] is None or record[4]['kind'] == group['kind'])]
        if not rows:
            continue
        # A sweep's x axis must vary only the workload dimension; one figure
        # never pools different parameters or sources.
        identities = {(parameters, source) for _, parameters, source, _, _ in rows}
        if len(identities) > 1:
            raise ValueError(
                f'clone {group["suffix"]} sweep mixes {len(identities)} configurations: '
                f'{describe_identities(identities)}; '
                'plot one with --parameters <json> and/or --source-sha256 <hash>[,<hash>]')
        figures.append((group['suffix'], clone_line_data(
            group, [(metrics, details) for _, _, _, metrics, details in rows], series, args)))
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


def mdtest_plot_data(connection, args: argparse.Namespace) -> list[tuple[str, dict]]:
    """Compare fixed-work metadata throughput at matching process counts."""
    if args.metric != 'ops_per_s':
        raise ValueError('mdtest concurrency figures require --metric ops_per_s')
    rows = connection.execute('''
        SELECT target->>'name', "case",
               parameter_canonical.canonical, source_sha256,
               parameters::VARCHAR, metrics::VARCHAR, details::VARCHAR
        FROM measurements
        LEFT JOIN parameter_canonical
               ON CAST(measurements.parameters AS VARCHAR) = parameter_canonical.raw
        WHERE benchmark = 'mdtest'
        ORDER BY run_id, repeat, target->>'name', "case"
    ''').fetchall()
    wanted_parameters = getattr(args, 'parameters', None)
    wanted_sources = getattr(args, 'source_sha256', None)
    rows = filter_identities(rows, wanted_parameters, wanted_sources,
                             lambda row: (row[2], row[3]))
    if not rows:
        raise ValueError('no mdtest measurements match the plot selection'
                         + selection_detail(wanted_parameters, wanted_sources))
    targets = args.targets or sorted({row[0] for row in rows})
    cases = args.cases or sorted({row[1] for row in rows})
    for kind, wanted, found in (
        ('targets', targets, {row[0] for row in rows}),
        ('cases', cases, {row[1] for row in rows}),
    ):
        missing = set(wanted) - found
        if missing:
            raise ValueError(f'no mdtest measurements for {kind}: {", ".join(sorted(missing))}')
    selected = [(row[0], (row[2], row[3]), json.loads(row[4]),
                 json.loads(row[5]), json.loads(row[6]))
                for row in rows if row[0] in targets and row[1] in cases]
    figures = []
    for operation, title in MDTEST_OPERATIONS.items():
        for layout in ('private', 'shared'):
            group = [entry for entry in selected
                     if entry[4]['operation'] == operation and entry[4]['layout'] == layout]
            if not group:
                continue
            suffix = f'{operation}-{layout}'
            conditions = {
                tuple(parameters[key] for key in
                      ('total_items', 'mdtest_version', 'cache_state', 'completion'))
                for _, _, parameters, _, _ in group
            }
            if len(conditions) != 1:
                raise ValueError(f'mdtest {suffix} mixes incompatible experiment conditions')
            identities = {identity for _, identity, _, _, _ in group}
            if len(identities) > 1:
                raise ValueError(
                    f'mdtest {suffix} mixes {len(identities)} configurations: '
                    f'{describe_identities(identities)}; '
                    'plot one with --parameters <json> and/or --source-sha256 <hash>[,<hash>]')
            points = {target: {} for target in targets}
            for target, _, _, metrics, _ in group:
                x = metrics['concurrency']
                points[target].setdefault(x, []).append(metrics['ops_per_s'] / 1000)
            missing = [target for target in targets if not points[target]]
            if missing:
                raise ValueError(f'no mdtest {suffix} measurements for targets: {", ".join(missing)}')
            expected = set(points[targets[0]])
            if any(set(points[target]) != expected for target in targets):
                raise ValueError(f'mdtest {suffix} targets must cover the same x values')
            figures.append((suffix, {
                'series': targets,
                'points': {target: [{'x': x, 'values': sorted(points[target][x])}
                                    for x in sorted(points[target])] for target in targets},
                'title': f'{title} ({layout})',
                'xlabel': 'Client processes',
                'ylabel': args.ylabel or 'Throughput (Kops/s)',
                'logx': True, 'logy': False, 'size': list(args.size),
            }))
    if not figures:
        raise ValueError('no mdtest measurements match the plot selection')
    return figures
