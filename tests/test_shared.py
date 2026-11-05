import contextlib
import io
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import clone
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
                    run_benchmark(['clone', '--dirs', str(target)])
            self.assertEqual(list(outside.iterdir()), [])


class PortableResultsTests(unittest.TestCase):
    def emit(self, output, parameters, repeat=1, *, run_id='run-b', value=None,
             benchmark='example', target='fs-a', case='write', source='source'):
        context = RunContext(benchmark, run_id, Target(target, Path('/mnt/private')),
                             Path('/missing/work'), Path('/missing/results'),
                             parameters, source, output)
        context.emit(case, {'latency_ms': repeat * 2.0 if value is None else value},
                     repeat=repeat,
                     details={'samples_ms': [1.0, 3.0], 'nested': {'mode': 'FULL'}})

    def test_global_summary_retains_runs_and_portable_raw_samples(self):
        # The only transferred artifact is JSONL; no target, log, or source paths exist.
        output = io.StringIO()
        self.emit(output, {'fsync': True, 'size': 4}, 1)
        self.emit(output, {'size': 4, 'fsync': True}, 2)
        self.emit(output, {'size': 8, 'fsync': True}, run_id='run-a', value=6.0,
                  source='another-source')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.jsonl'
            path.write_text(output.getvalue())
            connection = connect([str(path)])
            self.addCleanup(connection.close)
            groups = connection.execute(SUMMARY_SQL).fetchall()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0][:9], ('fs-a', 'write', 'latency_ms', 3,
                                            4.0, 4.0, 2.0, 6.0, 2.0))
            self.assertEqual(groups[0][9:], ('example', 'run-a;run-b'))
            samples = connection.execute(
                "SELECT details->'samples_ms' FROM measurements LIMIT 1").fetchone()[0]
            self.assertEqual(json.loads(samples), [1.0, 3.0])
            database = Path(directory) / 'analysis.duckdb'
            archived = connect([str(path)], database)
            archived.close()
            summary = Path(directory) / 'summary.csv'
            self.assertEqual(export_summary(path, summary), 1)
            with summary.open(newline='') as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(exported[0]['run_ids'], 'run-a;run-b')
            self.assertEqual(int(exported[0]['samples']), 3)
            path.unlink()
            import duckdb
            with duckdb.connect(str(database)) as reopened:
                self.assertEqual(reopened.execute('SELECT sum(value) FROM metrics').fetchone()[0], 12.0)

    def test_global_summary_keeps_benchmarks_targets_cases_and_metrics_separate(self):
        output = io.StringIO()
        self.emit(output, {}, value=2.0)
        self.emit(output, {}, benchmark='other', value=7.0)
        self.emit(output, {}, target='fs-b', value=11.0)
        self.emit(output, {}, case='read', value=13.0)
        context = RunContext('example', 'run-b', Target('fs-a', Path('/mnt/private')),
                             Path('/work'), Path('/results'), {}, 'source', output)
        context.emit('write', {'throughput_mib_s': 17.0})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.jsonl'
            path.write_text(output.getvalue())
            connection = connect([str(path), str(Path(directory) / '*.jsonl')])
            self.addCleanup(connection.close)
            rows = connection.execute(SUMMARY_SQL).fetchall()
        observed = {(row[9], row[0], row[1], row[2]): (row[3], row[4]) for row in rows}
        self.assertEqual(observed, {
            ('example', 'fs-a', 'write', 'latency_ms'): (1, 2.0),
            ('other', 'fs-a', 'write', 'latency_ms'): (1, 7.0),
            ('example', 'fs-b', 'write', 'latency_ms'): (1, 11.0),
            ('example', 'fs-a', 'read', 'latency_ms'): (1, 13.0),
            ('example', 'fs-a', 'write', 'throughput_mib_s'): (1, 17.0),
        })

    def test_nonfinite_measurement_does_not_corrupt_jsonl(self):
        output = io.StringIO()
        self.emit(output, {'size': 4})
        before = output.getvalue()
        context = RunContext('example', 'run', Target('a', Path('/mnt/a')),
                             Path('/work'), Path('/results'), {}, 'source', output)
        with self.assertRaises(ValueError):
            context.emit('write', {'latency_ms': float('nan')})
        self.assertEqual(output.getvalue(), before)


class SummaryRunTests(unittest.TestCase):
    """Exercise real tree copies and their derived CSV lifecycle."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.targets = [self.root / 'a', self.root / 'b']
        for target in self.targets:
            target.mkdir()
        self.enterContext(mock.patch('agent_fs_bench.cli.RESULTS_DIR', self.root / 'results'))
        self.enterContext(mock.patch.dict(clone.CONFIG, {
            'stage': 'single', 'repeats': 1, 'small_count': 2,
            'kinds': ('small',), 'copiers': ['internal'], 'fsync': False,
        }))

    def invoke(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run_benchmark(['clone', '--dirs', *map(str, self.targets)])

    def run_dir(self):
        return next((self.root / 'results' / 'clone').iterdir())

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
            self.assertEqual(row['run_ids'], run_dir.name)

    def test_summary_write_failure_preserves_measurements_without_rerunning(self):
        measure = clone.run

        def block_csv(ctx, args):
            measure(ctx, args)
            (ctx.result_dir.parent / 'summary.csv').mkdir(exist_ok=True)

        with mock.patch.object(clone, 'run', block_csv):
            self.assertEqual(self.invoke(), 1)
        records = [json.loads(line) for line in (self.run_dir() / 'results.jsonl').read_text().splitlines()]
        self.assertEqual([row['target']['name'] for row in records], ['a', 'a', 'b', 'b'])
        self.assertTrue((self.run_dir() / 'summary.csv').is_dir())

    def test_failed_workload_does_not_export_summary(self):
        with mock.patch.dict(clone.CONFIG, {'copiers': ['missing']}):
            with self.assertRaises(clone.BenchmarkError):
                self.invoke()
        self.assertEqual((self.run_dir() / 'results.jsonl').read_text(), '')
        self.assertFalse((self.run_dir() / 'summary.csv').exists())

    def test_later_target_failure_keeps_earlier_rows_without_summary(self):
        measure = clone.run

        def block_second_scratch(ctx, args):
            if ctx.target.name == 'b':
                (ctx.work_dir / 'scratch').write_text('not a directory')
            measure(ctx, args)

        with mock.patch.object(clone, 'run', block_second_scratch):
            with self.assertRaises(NotADirectoryError):
                self.invoke()
        records = [json.loads(line) for line in (self.run_dir() / 'results.jsonl').read_text().splitlines()]
        self.assertEqual([row['target']['name'] for row in records], ['a', 'a'])
        self.assertFalse((self.run_dir() / 'summary.csv').exists())


if __name__ == '__main__':
    unittest.main()
