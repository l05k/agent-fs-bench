"""Offline DuckDB queries and plotting over portable JSONL measurements."""

import argparse
import csv
import glob
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
       item.key AS metric, CAST(item.value AS DOUBLE) AS value
FROM measurements, json_each(metrics) AS item
'''
SUMMARY_SQL = '''
SELECT target, "case", metric, count(*) AS samples, avg(value) AS mean,
       median(value) AS median, min(value) AS minimum, max(value) AS maximum,
       stddev_samp(value) AS stddev, benchmark,
       string_agg(DISTINCT run_id, ';' ORDER BY run_id) AS run_ids
FROM metrics
GROUP BY benchmark, target, "case", metric
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
    script = stem.with_name(stem.name + '.py')
    script.write_text(
        Path(figure.__file__).read_text(encoding='utf-8')
        + f'\n\nDATA = {data!r}\n\nif __name__ == "__main__":\n'
        + '    render(DATA, Path(__file__).with_suffix(""))\n',
        encoding='utf-8')
    figure.render(data, stem)
