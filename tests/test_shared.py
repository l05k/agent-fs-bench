import contextlib
import io
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import clone
from agent_fs_bench.analysis import (SUMMARY_SQL, connect, export_summary,
                                     main as analysis_main)
from agent_fs_bench.cli import run_benchmark
from agent_fs_bench.results import RunContext
from agent_fs_bench.targets import Target, load_targets


class TargetTests(unittest.TestCase):
    def test_named_directories_load_complete_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'targets.toml'
            config.write_text('[[targets]]\nname="a"\npath="fs-a"\n'
                              '[[targets]]\nname="b"\npath="/scratch/fs-b"\n')
            self.assertEqual(load_targets(config, None), [
                Target('a', Path('/mnt/fs-a')),
                Target('b', Path('/scratch/fs-b')),
            ])

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


class CloneTargetSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / 'lakebase'
        self.target.mkdir()
        self.config = self.root / 'targets.toml'
        self.config.write_text(
            f'[[targets]]\nname="LakeBase"\npath="{self.target}"\n'
            f'[[targets]]\nname="NFS"\npath="{self.root / "nfs"}"\n'
        )
        self.results = self.root / 'results'
        self.enterContext(mock.patch('agent_fs_bench.cli.TARGETS_FILE', self.config))
        self.enterContext(mock.patch('agent_fs_bench.cli.RESULTS_DIR', self.results))

    def test_unknown_copier_alias_fails_before_creating_run(self):
        for target_args, alias in ((['--target', 'LakeBase'], 'Lakebase'), (['--dirs', str(self.target)], 'LakeBase')):
            with self.subTest(target_args=target_args), mock.patch.dict(
                clone.CONFIG, {'target_copiers': {alias: ['lakebase']}},
            ):
                with self.assertRaises(ValueError):
                    run_benchmark(['clone', *target_args, '--', '--smoke'])
                self.assertFalse(self.results.exists())
                self.assertEqual(list(self.target.iterdir()), [])

    def test_unknown_selected_target_fails_before_creating_run(self):
        with self.assertRaises(ValueError):
            run_benchmark(['clone', '--target', 'missing', '--', '--smoke'])
        self.assertFalse(self.results.exists())

    def test_selected_native_copier_accepts_valid_unselected_alias(self):
        with (
            mock.patch.dict(clone.CONFIG, {
                'target_copiers': {'LakeBase': ['test-native'], 'NFS': ['uninstalled']},
                'commands': {'test-native': 'cp -r {src} {dst}'},
            }),
            mock.patch.object(clone, 'fstype', return_value='tmpfs'),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = run_benchmark(['clone', '--target', 'LakeBase', '--', '--smoke'])
        self.assertEqual(status, 0)
        jsonl = next(self.results.rglob('results.jsonl'))
        records = [json.loads(line) for line in jsonl.read_text().splitlines()]
        self.assertEqual({(row['target']['name'], row['details']['copier']) for row in records},
                         {('LakeBase', 'test-native')})
        self.assertFalse((self.root / 'nfs').exists())


class PortableResultsTests(unittest.TestCase):
    def emit(self, output, parameters, repeat=1, *, run_id='run-b', value=None,
             benchmark='example', target='fs-a', case='write', source='source'):
        context = RunContext(benchmark, run_id, Target(target, Path('/mnt/private')),
                             Path('/missing/work'), Path('/missing/results'),
                             parameters, source, output)
        context.emit(case, {'latency_ms': repeat * 2.0 if value is None else value},
                     repeat=repeat,
                     details={'samples_ms': [1.0, 3.0], 'nested': {'mode': 'FULL'}})

    def summary_rows(self, connection):
        cursor = connection.execute(SUMMARY_SQL)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def test_summary_canonicalizes_parameters_and_separates_configurations(self):
        # Parameter key order, including nested objects, must not create a new group.
        output = io.StringIO()
        self.emit(output, {'fsync': True, 'nested': {'b': 2, 'a': 1}, 'size': 4}, 1, value=2.0)
        self.emit(output, {'size': 4, 'nested': {'a': 1, 'b': 2}, 'fsync': True}, 2, value=4.0)
        self.emit(output, {'fsync': True, 'nested': {'b': 2, 'a': 1}, 'size': 8},
                  run_id='run-a', value=6.0)
        self.emit(output, {'fsync': True, 'nested': {'b': 2, 'a': 1}, 'size': 4},
                  run_id='run-a', source='other-source', value=8.0)
        canonical = '{"fsync":true,"nested":{"a":1,"b":2},"size":4}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.jsonl'
            path.write_text(output.getvalue())
            connection = connect([str(path)])
            self.addCleanup(connection.close)
            rows = self.summary_rows(connection)
            summary = Path(directory) / 'summary.csv'
            self.assertEqual(export_summary(path, summary), 3)
            with summary.open(newline='') as handle:
                exported = list(csv.DictReader(handle))
        groups = {(row['parameters'], row['source_sha256']): row for row in rows}
        self.assertEqual(set(groups), {
            (canonical, 'source'),
            ('{"fsync":true,"nested":{"a":1,"b":2},"size":8}', 'source'),
            (canonical, 'other-source'),
        })
        combined = groups[(canonical, 'source')]
        self.assertEqual(
            (combined['samples'], combined['mean'], combined['median'],
             combined['minimum'], combined['maximum'], combined['run_ids']),
            (2, 3.0, 3.0, 2.0, 4.0, 'run-b'))
        self.assertAlmostEqual(combined['stddev'], 2 ** 0.5)
        self.assertEqual({(row['parameters'], row['source_sha256']) for row in exported},
                         set(groups))
        self.assertEqual(sum(int(row['samples']) for row in exported), 4)

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
            rows = self.summary_rows(connection)
        observed = {(row['benchmark'], row['target'], row['case'], row['metric']):
                    (row['samples'], row['mean']) for row in rows}
        self.assertEqual(observed, {
            ('example', 'fs-a', 'write', 'latency_ms'): (1, 2.0),
            ('other', 'fs-a', 'write', 'latency_ms'): (1, 7.0),
            ('example', 'fs-b', 'write', 'latency_ms'): (1, 11.0),
            ('example', 'fs-a', 'read', 'latency_ms'): (1, 13.0),
            ('example', 'fs-a', 'write', 'throughput_mib_s'): (1, 17.0),
        })

    def test_portable_archive_preserves_identity_and_raw_samples(self):
        output = io.StringIO()
        self.emit(output, {'fsync': True, 'size': 4}, 1, value=2.0)
        self.emit(output, {'size': 8, 'fsync': True}, 2, value=6.0, run_id='run-a')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.jsonl'
            path.write_text(output.getvalue())
            database = Path(directory) / 'analysis.duckdb'
            connect([str(path)], database).close()
            path.unlink()
            import duckdb
            with duckdb.connect(str(database)) as reopened:
                self.assertEqual(
                    reopened.execute('SELECT sum(value) FROM metrics').fetchone()[0], 8.0)
                rows = self.summary_rows(reopened)
                samples = reopened.execute(
                    "SELECT details->'samples_ms' FROM measurements LIMIT 1").fetchone()[0]
                self.assertEqual(json.loads(samples), [1.0, 3.0])
        self.assertEqual({row['parameters'] for row in rows},
                         {'{"fsync":true,"size":4}', '{"fsync":true,"size":8}'})
        self.assertEqual(sum(row['samples'] for row in rows), 2)

    def test_nonfinite_measurement_does_not_corrupt_jsonl(self):
        output = io.StringIO()
        self.emit(output, {'size': 4})
        before = output.getvalue()
        context = RunContext('example', 'run', Target('a', Path('/mnt/a')),
                             Path('/work'), Path('/results'), {}, 'source', output)
        with self.assertRaises(ValueError):
            context.emit('write', {'latency_ms': float('nan')})
        self.assertEqual(output.getvalue(), before)


class PlotSelectionTests(unittest.TestCase):
    """Check plot data selection without rendering real graphics."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = io.StringIO()

    def emit(self, target, case, parameters, *, value, source='source',
             run_id='run-b', repeat=1):
        context = RunContext('bench', run_id, Target(target, Path('/mnt') / target),
                             Path('/work'), Path('/results'), parameters, source,
                             self.output)
        context.emit(case, {'latency_ms': value}, repeat=repeat)

    def arguments(self, *extra):
        path = self.root / 'data.jsonl'
        path.write_text(self.output.getvalue())
        return [str(path), '--benchmark', 'bench', '--metric', 'latency_ms',
                '--output', str(self.root / 'figure'), *extra]

    def render(self, *extra):
        with mock.patch('agent_fs_bench.figure.render') as render:
            self.assertEqual(analysis_main('plot', self.arguments(*extra)), 0)
        return render.call_args.args[0]

    def test_rejects_mixed_identity_across_targets_in_one_case(self):
        self.emit('fs-a', 'c1', {'size': 4}, value=1.0)
        self.emit('fs-b', 'c1', {'size': 8}, value=2.0)
        with mock.patch('agent_fs_bench.figure.render') as render:
            with self.assertRaisesRegex(ValueError, 'mixes 2 configurations') as caught:
                analysis_main('plot', self.arguments())
        render.assert_not_called()
        message = str(caught.exception)
        self.assertIn('--parameters', message)
        self.assertIn('--source-sha256', message)
        self.assertIn('"size":4', message)
        self.assertIn('"size":8', message)

    def test_parameters_selector_chooses_one_configuration(self):
        self.emit('fs-a', 'c1', {'size': 4, 'fsync': True}, value=1.0)
        self.emit('fs-b', 'c1', {'fsync': True, 'size': 8}, value=2.0)
        self.emit('fs-b', 'c1', {'fsync': True, 'size': 4}, value=3.0, run_id='run-a')
        data = self.render('--parameters', '{"fsync": true, "size": 4}')
        self.assertEqual(data['targets'], ['fs-a', 'fs-b'])
        self.assertEqual(data['samples'],
                         {'fs-a': {'c1': [1.0]}, 'fs-b': {'c1': [3.0]}})

    def test_source_selector_separates_equal_parameters(self):
        self.emit('fs-a', 'c1', {'size': 4}, value=1.0, source='source-a')
        self.emit('fs-b', 'c1', {'size': 4}, value=2.0, source='source-b')
        with mock.patch('agent_fs_bench.figure.render') as render:
            with self.assertRaisesRegex(ValueError, 'mixes 2 configurations'):
                analysis_main('plot', self.arguments())
        render.assert_not_called()
        data = self.render('--source-sha256', 'source-b')
        self.assertEqual(data['targets'], ['fs-b'])
        self.assertEqual(data['samples'], {'fs-b': {'c1': [2.0]}})

    def test_target_and_case_filters_run_before_identity_check(self):
        self.emit('fs-a', 'c1', {'size': 4}, value=1.0)
        self.emit('fs-b', 'c1', {'size': 8}, value=2.0)
        self.emit('fs-a', 'c2', {'size': 8}, value=3.0)
        self.emit('fs-b', 'c2', {'size': 8}, value=4.0)
        self.assertEqual(self.render('--targets', 'fs-a')['samples'],
                         {'fs-a': {'c1': [1.0], 'c2': [3.0]}})
        self.assertEqual(self.render('--cases', 'c2')['samples'],
                         {'fs-a': {'c2': [3.0]}, 'fs-b': {'c2': [4.0]}})

    def test_distinct_configurations_in_distinct_cases_are_plotted(self):
        self.emit('fs-a', 'c1', {'size': 4}, value=1.0)
        self.emit('fs-a', 'c2', {'size': 8}, value=2.0)
        data = self.render()
        self.assertEqual(data['cases'], ['c1', 'c2'])
        self.assertEqual(data['samples'], {'fs-a': {'c1': [1.0], 'c2': [2.0]}})

    def test_same_identity_aggregates_across_runs(self):
        self.emit('fs-a', 'c1', {'size': 4}, value=2.0, run_id='run-b')
        self.emit('fs-a', 'c1', {'size': 4}, value=1.0, run_id='run-a')
        self.assertEqual(self.render()['samples'], {'fs-a': {'c1': [1.0, 2.0]}})

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
            'stage': 'bytes', 'repeats': 1,
            'byte_totals': (8192, 16384), 'byte_files': 2,
        }))
        self.enterContext(mock.patch.object(clone, 'fstype', return_value='tmpfs'))

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
        expected = {(f"{row['target']['name']}/{row['details']['copier']}",
                     row['case'], metric): value
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
            with self.assertRaises(OSError):
                self.invoke()
        records = [json.loads(line) for line in (self.run_dir() / 'results.jsonl').read_text().splitlines()]
        self.assertEqual([row['target']['name'] for row in records], ['a', 'a'])
        self.assertFalse((self.run_dir() / 'summary.csv').exists())


if __name__ == '__main__':
    unittest.main()
