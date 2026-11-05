"""Offline DuckDB queries and plotting over portable JSONL measurements."""

import argparse
import csv
import glob
from pathlib import Path
import sys
from typing import TextIO

COLUMNS = {
    'benchmark': 'VARCHAR', 'run_id': 'VARCHAR', 'timestamp': 'VARCHAR',
    'target': 'JSON', 'case': 'VARCHAR', 'repeat': 'BIGINT',
    'parameters': 'JSON', 'parameter_id': 'VARCHAR', 'environment': 'JSON',
    'metrics': 'JSON', 'details': 'JSON',
}
METRICS_SQL = '''
SELECT benchmark, run_id, timestamp, target->>'name' AS target,
       target->>'path' AS target_path, "case", repeat, parameter_id, parameters,
       environment->>'source_sha256' AS source_sha256,
       environment->>'hostname' AS hostname,
       item.key AS metric, CAST(item.value AS DOUBLE) AS value
FROM measurements, json_each(metrics) AS item
'''
SUMMARY_SQL = '''
SELECT target, "case", metric, count(*) AS samples, avg(value) AS mean,
       median(value) AS median, min(value) AS minimum, max(value) AS maximum,
       stddev_samp(value) AS stddev, parameter_id, benchmark, target_path,
       source_sha256, hostname
FROM metrics
GROUP BY ALL
ORDER BY benchmark, "case", metric, target
'''


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
        parser.add_argument('--case')
        parser.add_argument('--parameter-id', help='Restrict to one exact workload configuration')
        parser.add_argument('--output', type=Path, required=True, help='Output PNG, SVG or PDF')
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
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    conditions = ['benchmark = ?', 'metric = ?']
    values = [args.benchmark, args.metric]
    if args.case:
        conditions.append('"case" = ?')
        values.append(args.case)
    if args.parameter_id:
        conditions.append('parameter_id = ?')
        values.append(args.parameter_id)
    rows = connection.execute('''
        SELECT target, target_path, "case", parameter_id, source_sha256, hostname,
               list(value ORDER BY run_id, repeat)
        FROM metrics WHERE ''' + ' AND '.join(conditions) + '''
        GROUP BY ALL ORDER BY "case", parameter_id, source_sha256, hostname, target
    ''', values).fetchall()
    if not rows:
        raise ValueError('no measurements match the plot selection')
    # Keep distinct workload parameters, source revisions and hosts separate.
    groups = {(row[3], row[4], row[5]) for row in rows}
    target_paths = {}
    for row in rows:
        target_paths.setdefault(row[0], set()).add(row[1])
    labels = [f'{row[0]} / {row[2]}' +
              (f'\n{row[1]}' if len(target_paths[row[0]]) > 1 else '') +
              (f'\n{row[3][:8]} {row[4][:8]} {row[5]}' if len(groups) > 1 else '')
              for row in rows]
    figure, axes = plt.subplots(figsize=(max(7, len(rows) * 1.4), 5), layout='constrained')
    axes.boxplot([row[6] for row in rows], tick_labels=labels, showmeans=True)
    axes.set_title(args.benchmark)
    axes.set_ylabel(args.metric)
    axes.tick_params(axis='x', labelrotation=25)
    axes.grid(axis='y', alpha=0.3)
    figure.savefig(args.output)
    plt.close(figure)
