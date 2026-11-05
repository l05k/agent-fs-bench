import contextlib
import io
import csv
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import openclaw_session
from agent_fs_bench.analysis import SUMMARY_SQL, connect, export_summary
from agent_fs_bench.cli import run_benchmark
from agent_fs_bench.results import RunContext
from agent_fs_bench.targets import Target, load_targets


class TargetTests(unittest.TestCase):
    def test_named_directories_and_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'targets.toml'
            config.write_text('[[targets]]\nname="a"\npath="fs-a"\n'
                              '[[targets]]\nname="b"\npath="/scratch/fs-b"\n')
            self.assertEqual(load_targets(config, None, ['a']),
                             [Target('a', Path('/mnt/fs-a'))])
            with self.assertRaisesRegex(ValueError, 'unknown targets'):
                load_targets(config, None, ['missing'])

    def test_ambiguous_and_escaping_directories_rejected(self):
        for directories in (['/mnt/a', '/other/a'], ['/mnt/a', '/mnt/a'],
                            ['../a'], ['/'], []):
            with self.subTest(directories=directories), self.assertRaises(ValueError):
                load_targets(None, directories)

    def test_symlink_cannot_redirect_managed_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, outside = root / 'target', root / 'outside'
            target.mkdir()
            outside.mkdir()
            (target / 'agent-fs-bench').symlink_to(outside, target_is_directory=True)
            with mock.patch('agent_fs_bench.cli.RESULTS_DIR', root / 'results'):
                with self.assertRaisesRegex(ValueError, 'escapes target'):
                    run_benchmark(['openclaw-session', '--dirs', str(target)])
            self.assertEqual(list(outside.iterdir()), [])


class PortableResultsTests(unittest.TestCase):
    def emit(self, output, parameters, repeat=1):
        context = RunContext('example', 'run', Target('fs-a', Path('/mnt/private')),
                             Path('/missing/work'), Path('/missing/results'),
                             parameters, {'source_sha256': 'source', 'hostname': 'host'}, output)
        context.emit('write', {'latency_ms': repeat * 2.0}, repeat=repeat,
                     details={'samples_ms': [1.0, 3.0], 'nested': {'mode': 'FULL'}})

    def test_portable_nested_data_and_parameter_groups(self):
        # The only transferred artifact is JSONL; no target, log, or source paths exist.
        output = io.StringIO()
        self.emit(output, {'fsync': True, 'size': 4}, 1)
        self.emit(output, {'size': 4, 'fsync': True}, 2)
        self.emit(output, {'size': 8, 'fsync': True}, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.jsonl'
            path.write_text(output.getvalue())
            connection = connect([str(path)])
            self.addCleanup(connection.close)
            groups = connection.execute(SUMMARY_SQL).fetchall()
            self.assertEqual(sorted((row[3], row[4]) for row in groups), [(1, 2.0), (2, 3.0)])
            samples = connection.execute(
                "SELECT details->'samples_ms' FROM measurements LIMIT 1").fetchone()[0]
            self.assertEqual(json.loads(samples), [1.0, 3.0])
            database = Path(directory) / 'analysis.duckdb'
            archived = connect([str(path)], database)
            archived.close()
            summary = Path(directory) / 'summary.csv'
            self.assertEqual(export_summary(path, summary), 2)
            header = summary.read_text().splitlines()[0]
            self.assertEqual(
                header,
                'target,case,metric,samples,mean,median,minimum,maximum,stddev,'
                'parameter_id,benchmark,target_path,source_sha256,hostname',
            )
            path.unlink()
            import duckdb
            with duckdb.connect(str(database)) as reopened:
                self.assertEqual(reopened.execute('SELECT sum(value) FROM metrics').fetchone()[0], 8.0)

    def test_nonfinite_measurement_does_not_corrupt_jsonl(self):
        output = io.StringIO()
        self.emit(output, {'size': 4})
        before = output.getvalue()
        context = RunContext('example', 'run', Target('a', Path('/mnt/a')),
                             Path('/work'), Path('/results'), {}, {}, output)
        with self.assertRaises(ValueError):
            context.emit('write', {'latency_ms': float('nan')})
        self.assertEqual(output.getvalue(), before)


class SummaryRunTests(unittest.TestCase):
    """Exercise real SQLite measurements and their derived CSV lifecycle."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.targets = [self.root / 'a', self.root / 'b']
        for target in self.targets:
            target.mkdir()
        self.enterContext(mock.patch('agent_fs_bench.cli.RESULTS_DIR', self.root / 'results'))
        self.enterContext(mock.patch.dict(openclaw_session.CONFIG, {
            'duration': 0.03, 'rate': 0, 'sessions': 1,
            'prefill_events': 0, 'no_fts': True,
        }))

    def invoke(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run_benchmark(['openclaw-session', '--dirs', *map(str, self.targets)])

    def run_dir(self):
        return next((self.root / 'results' / 'openclaw-session').iterdir())

    def test_default_summary_contains_all_measured_targets(self):
        self.assertEqual(self.invoke(), 0)
        run_dir = self.run_dir()
        records = [json.loads(line) for line in (run_dir / 'results.jsonl').read_text().splitlines()]
        self.assertEqual({row['target']['name'] for row in records}, {'a', 'b'})
        expected = {(row['target']['name'], row['case'], metric): value
                    for row in records for metric, value in row['metrics'].items()}
        with (run_dir / 'summary.csv').open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({(row['target'], row['case'], row['metric']) for row in rows}, set(expected))
        for row in rows:
            key = row['target'], row['case'], row['metric']
            self.assertEqual(int(row['samples']), 1)
            self.assertEqual(float(row['mean']), expected[key])
            self.assertEqual(float(row['median']), expected[key])

    def test_summary_write_failure_preserves_measurements_without_rerunning(self):
        measure = openclaw_session.run

        def block_csv(ctx, args):
            measure(ctx, args)
            (ctx.result_dir.parent / 'summary.csv').mkdir(exist_ok=True)

        with mock.patch.object(openclaw_session, 'run', block_csv):
            self.assertEqual(self.invoke(), 1)
        records = [json.loads(line) for line in (self.run_dir() / 'results.jsonl').read_text().splitlines()]
        self.assertEqual([row['target']['name'] for row in records], ['a', 'b'])
        self.assertTrue(all(row['details']['integrity_check'] == 'ok' for row in records))
        self.assertTrue((self.run_dir() / 'summary.csv').is_dir())

    def test_failed_workload_does_not_export_summary(self):
        with mock.patch.dict(openclaw_session.CONFIG, {'pragma': ('unsupported=1',)}):
            with self.assertRaises(openclaw_session.BenchmarkError):
                self.invoke()
        self.assertEqual((self.run_dir() / 'results.jsonl').read_text(), '')
        self.assertFalse((self.run_dir() / 'summary.csv').exists())

    def test_later_target_failure_keeps_earlier_rows_without_summary(self):
        measure = openclaw_session.run

        def block_second_database(ctx, args):
            if ctx.target.name == 'b':
                (ctx.work_dir / openclaw_session.DB_FILENAME).mkdir()
            measure(ctx, args)

        with mock.patch.object(openclaw_session, 'run', block_second_database):
            with self.assertRaises(sqlite3.OperationalError):
                self.invoke()
        records = [json.loads(line) for line in (self.run_dir() / 'results.jsonl').read_text().splitlines()]
        self.assertEqual([row['target']['name'] for row in records], ['a'])
        self.assertEqual(records[0]['details']['integrity_check'], 'ok')
        self.assertFalse((self.run_dir() / 'summary.csv').exists())


if __name__ == '__main__':
    unittest.main()
