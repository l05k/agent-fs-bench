import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent_fs_bench.analysis import canonical_parameter_text, connect, mdtest_plot_data
from agent_fs_bench.results import RunContext
from agent_fs_bench.targets import Target


class MdtestPlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = io.StringIO()
        self.args = SimpleNamespace(metric='ops_per_s', cases=None,
                                    targets=['ours', 'nfs'], ylabel=None, size=(3.5, 1.5))

    def emit(self, target, concurrency, rate, *, operation='file-create',
             layout='private', total=128, repeat=1, version='4.0.0', source='source'):
        context = RunContext(
            'mdtest', 'run-a', Target(target, Path('/mnt') / target),
            Path('/missing/work'), Path('/missing/results'),
            {'total_items': total, 'mdtest_version': version,
             'cache_state': 'create-then-access-no-drop', 'completion': 'operation-return'},
            source, self.output,
        )
        context.emit(
            f'{operation}/{layout}/N{total}/P{concurrency}',
            {'concurrency': concurrency, 'ops': total, 'elapsed_s': total / rate,
             'ops_per_s': rate}, repeat=repeat,
            details={'operation': operation, 'layout': layout},
        )

    def connection(self):
        path = self.root / 'results.jsonl'
        path.write_text(self.output.getvalue(), encoding='utf-8')
        connection = connect([str(path)])
        self.addCleanup(connection.close)
        return connection

    def test_operations_and_layouts_stay_separate_and_repeats_are_rate_samples(self):
        for target in self.args.targets:
            for concurrency in (4, 1, 2):
                self.emit(target, concurrency, 1000 * concurrency)
                self.emit(target, concurrency, 3000 * concurrency, repeat=2)
                self.emit(target, concurrency, 5000 * concurrency, layout='shared')
                self.emit(target, concurrency, 7000 * concurrency, operation='file-stat')
        figures = dict(mdtest_plot_data(self.connection(), self.args))
        self.assertEqual(set(figures), {'file-create-private', 'file-create-shared',
                                       'file-stat-private'})
        self.assertEqual(figures['file-create-private']['points']['ours'], [
            {'x': 1, 'values': [1.0, 3.0]},
            {'x': 2, 'values': [2.0, 6.0]},
            {'x': 4, 'values': [4.0, 12.0]},
        ])
        self.assertEqual(figures['file-create-shared']['points']['nfs'][0]['values'], [5.0])

    def test_partial_target_sweep_is_rejected(self):
        self.emit('ours', 1, 1000)
        self.emit('ours', 2, 2000)
        self.emit('nfs', 1, 500)
        with self.assertRaisesRegex(ValueError, 'same x values'):
            mdtest_plot_data(self.connection(), self.args)

    def test_target_missing_selected_operation_or_layout_is_identified(self):
        self.emit('ours', 1, 1000)
        self.emit('nfs', 1, 500, operation='file-stat')
        self.emit('nfs', 1, 500, layout='shared')
        self.args.cases = ['file-create/private/N128/P1']
        with self.assertRaises(ValueError) as error:
            mdtest_plot_data(self.connection(), self.args)
        self.assertIn('file-create-private', str(error.exception))
        self.assertIn('nfs', str(error.exception))
        self.assertNotIn('same x values', str(error.exception))

    def test_total_work_and_tool_version_cannot_be_mixed(self):
        for change in ({'total': 256}, {'version': '4.1.0'}):
            with self.subTest(change=change):
                self.output = io.StringIO()
                self.emit('ours', 1, 1000)
                self.emit('nfs', 1, 500, **change)
                with self.assertRaisesRegex(ValueError, 'incompatible experiment conditions'):
                    mdtest_plot_data(self.connection(), self.args)

    def test_filter_excludes_unselected_incompatible_runs(self):
        for target in self.args.targets:
            self.emit(target, 1, 1000)
            self.emit(target, 1, 5000, total=256)
        self.args.cases = ['file-create/private/N128/P1']
        figures = mdtest_plot_data(self.connection(), self.args)
        self.assertEqual(figures[0][1]['points']['ours'], [{'x': 1, 'values': [1.0]}])

    def test_parameters_selector_keeps_one_configuration(self):
        for target in self.args.targets:
            self.emit(target, 1, 1000)
            self.emit(target, 1, 5000, total=256)
        self.args.parameters = canonical_parameter_text(json.dumps({
            'total_items': 128, 'mdtest_version': '4.0.0',
            'cache_state': 'create-then-access-no-drop',
            'completion': 'operation-return'}))
        figures = mdtest_plot_data(self.connection(), self.args)
        self.assertEqual(figures[0][1]['points']['nfs'], [{'x': 1, 'values': [1.0]}])

    def test_source_selector_separates_same_conditions_from_two_sources(self):
        for target in self.args.targets:
            self.emit(target, 1, 1000, source='source-a')
            self.emit(target, 1, 5000, source='source-b')
        with self.assertRaisesRegex(ValueError, 'mixes 2 configurations'):
            mdtest_plot_data(self.connection(), self.args)
        self.args.source_sha256 = ['source-b']
        figures = mdtest_plot_data(self.connection(), self.args)
        self.assertEqual(figures[0][1]['points']['ours'], [{'x': 1, 'values': [5.0]}])

    def test_peak_sizes_stay_separate_and_bandwidth_skips_empty_files(self):
        for target in self.args.targets:
            for size in (0, 4096):
                context = RunContext(
                    'mdtest-peak', 'run-a', Target(target, Path('/mnt') / target),
                    Path('/missing/work'), Path('/missing/results'),
                    {'mdtest_version': '4.0.0', 'cache_state': 'c', 'completion': 'y'},
                    'source', self.output,
                )
                metrics = {'concurrency': 2, 'ops': 64, 'elapsed_s': 1.0, 'ops_per_s': 64.0}
                if size:
                    metrics.update(bytes=64 * size, mib_per_s=0.25)
                context.emit(f'file-create/private/S{size}/n32/P2', metrics, details={
                    'operation': 'file-create', 'layout': 'private',
                    'file_size_bytes': size, 'items_per_proc': 32})
        self.args.benchmark = 'mdtest-peak'
        connection = self.connection()
        self.assertEqual([suffix for suffix, _ in mdtest_plot_data(connection, self.args)],
                         ['file-create-private-S0', 'file-create-private-S4096'])
        self.args.metric = 'mib_per_s'
        figures = mdtest_plot_data(connection, self.args)
        self.assertEqual([suffix for suffix, _ in figures], ['file-create-private-S4096'])
        self.assertEqual(figures[0][1]['points']['ours'], [{'x': 2, 'values': [0.25]}])


if __name__ == '__main__':
    unittest.main()
