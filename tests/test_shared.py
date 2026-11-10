import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from agent_fs_bench import cluster
from agent_fs_bench.analysis import Figure, figure_data, plot
from agent_fs_bench.cli import run_benchmark
from agent_fs_bench.results import RunContext, Tables, _without_config, read_table, write_json
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


class TableTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tables = Tables(self.root)
        self.addCleanup(tables.close)
        self.ctx = RunContext('bench', 'run', Target('fs-a', self.root), self.root, self.root, tables)

    def test_rows_lead_with_target_and_keys(self):
        self.ctx.emit('phases', {'operation': 'create', 'fsync': True}, {'ops_per_s': 2.5})
        self.assertEqual(read_table(self.root / 'phases.csv'), [
            {'target': 'fs-a', 'operation': 'create', 'fsync': '1', 'ops_per_s': '2.5'}])

    def test_incomplete_or_non_finite_observations_are_rejected_before_writing(self):
        self.ctx.emit('phases', {'operation': 'create'}, {'ops_per_s': 1.0})
        for keys, metrics in (
            ({}, {'ops_per_s': 1.0}),
            ({'operation': 'stat'}, {'elapsed_s': 1.0}),
            ({'operation': 'stat'}, {'ops_per_s': float('nan')}),
            ({'operation': 'stat'}, {'ops_per_s': True}),
            ({'operation': 'stat'}, {'ops_per_s': None}),
            ({'operation': 'stat'}, {}),
        ):
            with self.subTest(keys=keys, metrics=metrics), self.assertRaises(AssertionError):
                self.ctx.emit('phases', keys, metrics)
        self.assertEqual(len(read_table(self.root / 'phases.csv')), 1)
        self.assertEqual(self.ctx.rows, 1)


def fake_workload(fail_on=None):
    def parse_args(argv):
        return SimpleNamespace(repeats=3, fixture=Path('/data/fixture'))

    def run(ctx, args):
        ctx.info['fstype'] = 'tmpfs'
        for procs in (1, 2):
            for repeat in range(1, args.repeats + 1):
                ctx.emit('phases', {'procs': procs, 'repeat': repeat},
                         {'ops_per_s': 10.0 * procs * repeat})
                if ctx.target.name == fail_on:
                    raise RuntimeError('mount lost')
                for sample in (1, 2):
                    ctx.emit('requests', {'procs': procs, 'repeat': repeat, 'sample': sample},
                             {'latency_s': float(sample)})
        ctx.emit('setup', {'step': 'build'}, {'elapsed_s': 3.0})

    return SimpleNamespace(parse_args=parse_args, run=run, FIGURES=[
        Figure('phases', 'procs', 'ops_per_s'),
        Figure('missing', 'procs', 'ops_per_s'),
    ])


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.results = self.root / 'results'
        self.dirs = [self.root / 'a', self.root / 'b']
        for directory in self.dirs:
            directory.mkdir()
        self.enterContext(mock.patch('agent_fs_bench.cli.RESULTS_DIR', self.results))
        self.render = self.enterContext(mock.patch('agent_fs_bench.figure.render'))
        # The runner probes the machines of --mode; these tests run no MPI.
        self.enterContext(mock.patch.object(cluster, 'open_pool', return_value=cluster.Pool(
            'local', (), cluster.Launcher(**cluster.CONFIG), '4.1.6')))
        self.enterContext(mock.patch.object(cluster, 'validate_pool'))
        self.enterContext(mock.patch('agent_fs_bench.cli.git_commit', return_value='abc123'))

    def invoke(self, module, *args):
        with (mock.patch('agent_fs_bench.analysis.workload', return_value=module),
              contextlib.redirect_stdout(io.StringIO())):
            return run_benchmark(['clone', *args])

    def run_dir(self):
        return next((self.results / 'clone').iterdir())

    def test_run_writes_config_snapshot_and_raw_tables_only(self):
        self.assertEqual(self.invoke(fake_workload(), '--dirs', *map(str, self.dirs)), 0)
        run = self.run_dir()
        snapshot = json.loads((run / 'config.json').read_text())
        self.assertEqual(snapshot['config'], {'repeats': 3, 'fixture': '/data/fixture',
                                              'mode': 'local',
                                              'launcher': cluster.Launcher(
                                                  **cluster.CONFIG).record()})
        self.assertEqual((snapshot['seed'], snapshot['git_commit']), (None, 'abc123'))
        self.assertEqual(snapshot['targets'], [
            {'name': 'a', 'path': str(self.dirs[0]), 'info': {'fstype': 'tmpfs'}},
            {'name': 'b', 'path': str(self.dirs[1]), 'info': {'fstype': 'tmpfs'}},
        ])
        self.assertEqual(len(read_table(run / 'phases.csv')), 12)
        self.assertEqual(len(read_table(run / 'requests.csv')), 24)
        self.assertFalse((run / 'summary').exists())
        self.assertFalse((run / 'figures').exists())
        self.render.assert_not_called()

    def test_failed_target_keeps_earlier_rows(self):
        with self.assertRaisesRegex(RuntimeError, 'mount lost'):
            self.invoke(fake_workload(fail_on='b'), '--dirs', *map(str, self.dirs))
        run = self.run_dir()
        snapshot = json.loads((run / 'config.json').read_text())
        self.assertEqual([target['name'] for target in snapshot['targets']], ['a'])
        self.assertEqual([row['target'] for row in read_table(run / 'phases.csv')], ['a'] * 6 + ['b'])

    def test_unknown_selected_target_fails_before_creating_run(self):
        config = self.root / 'targets.toml'
        config.write_text(f'[[targets]]\nname="a"\npath="{self.dirs[0]}"\n')
        with mock.patch('agent_fs_bench.cli.TARGETS_FILE', config), \
                self.assertRaises(AssertionError):
            self.invoke(fake_workload(), '--target', 'missing')
        self.assertFalse(self.results.exists())

    def test_misspelled_target_copier_alias_fails_before_creating_run(self):
        module = fake_workload()
        module.parse_args = lambda argv: SimpleNamespace(target_copiers={'LakeBase': ['lakebase']})
        with self.assertRaisesRegex(AssertionError, 'target_copiers aliases'):
            self.invoke(module, '--dirs', *map(str, self.dirs))
        self.assertFalse(self.results.exists())


def rows(*values):
    return [dict(zip(('target', 'operation', 'procs', 'repeat', 'ops_per_s'), map(str, value)))
            for value in values]


class FigureDataTests(unittest.TestCase):
    def test_line_pools_repeats_sorts_numeric_x_and_puts_primary_target_first(self):
        (suffix, data), = figure_data(Figure('t', 'procs', 'ops_per_s', yscale=1000), rows(
            ('b', 'create', 16, 1, 4000), ('a', 'create', 2, 1, 1000), ('a', 'create', 16, 1, 3000),
            ('a', 'create', 16, 2, 5000), ('b', 'create', 2, 1, ''),
        ), ['a', 'b'], (3.5, 1.5))
        self.assertEqual(suffix, '')
        self.assertEqual((data['series'], data['x']), (['a', 'b'], [2.0, 16.0]))
        self.assertEqual(data['values'], {'a': [[1.0], [3.0, 5.0]], 'b': [[], [4.0]]})

    def test_box_keeps_first_appearance_order_and_split_where_select_figures(self):
        spec = Figure('t', 'operation', 'ops_per_s', kind='box', split=('procs',), where={'repeat': 1})
        datasets = figure_data(spec, rows(
            ('a', 'stat', 4, 1, 2), ('a', 'create', 4, 1, 1), ('a', 'create', 4, 2, 9),
            ('a', 'stat', 1, 1, 3),
        ), ['a'], (3.5, 1.5))
        self.assertEqual([(suffix, data['title'], data['x'], data['values'])
                          for suffix, data in datasets], [
            ('-4', '4', ['stat', 'create'], {'a': [[2.0], [1.0]]}),
            ('-1', '1', ['stat'], {'a': [[3.0]]}),
        ])

    def test_legend_entries_match_full_series_labels_or_bare_targets(self):
        spec = Figure('t', 'procs', 'ops_per_s', series=('target', 'copier'))
        table = [dict(zip(('target', 'copier', 'procs', 'ops_per_s'), values)) for values in (
            ('a', 'cp-r', '1', '1'), ('a', 'native', '1', '2'), ('b', 'cp-r', '1', '3'))]
        for legend, series in ((['a/native', 'b/cp-r'], ['a/native', 'b/cp-r']),
                               (['b', 'a/cp-r'], ['b/cp-r', 'a/cp-r'])):
            with self.subTest(legend=legend):
                (_, data), = figure_data(spec, table, legend, (3.5, 1.5))
                self.assertEqual(data['series'], series)


class PlotRunsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.render = self.enterContext(mock.patch('agent_fs_bench.figure.render'))
        self.spec = Figure('phases', 'procs', 'ops_per_s')

    def make_run(self, name, target, value, repeats=3, source='s'):
        run = self.root / name
        run.mkdir()
        tables = Tables(run)
        RunContext('mdtest', name, Target(target, run), run, run, tables).emit(
            'phases', {'procs': 1, 'repeat': 1}, {'ops_per_s': value})
        tables.close()
        write_json(run / 'config.json', {'benchmark': 'mdtest', 'source_sha256': source,
                                         'config': {'repeats': repeats},
                                         'targets': [{'name': target}], 'tables': tables.schemas})
        return run

    def test_matching_runs_pool_in_run_order_unless_targets_reorder(self):
        runs = [self.make_run('r1', 'nfs', 1.0), self.make_run('r2', 'ours', 2.0),
                self.make_run('r3', 'ours', 4.0)]
        self.assertEqual(plot(runs, [self.spec], output=self.root / 'figures'),
                         [self.root / 'figures' / 'phases-ops_per_s'])
        data = self.render.call_args.args[0]
        self.assertEqual((data['series'], data['values']),
                         (['nfs', 'ours'], {'nfs': [[1.0]], 'ours': [[2.0, 4.0]]}))
        plot(runs, [self.spec], targets=['ours', 'nfs'], output=self.root / 'figures')
        self.assertEqual(self.render.call_args.args[0]['series'], ['ours', 'nfs'])

    def test_differing_configs_pool_only_for_declared_keys(self):
        runs = [self.make_run('r1', 'a', 1.0), self.make_run('r2', 'b', 2.0, repeats=5)]
        with self.assertRaisesRegex(AssertionError, 'repeats'):
            plot(runs, [self.spec])
        self.render.assert_not_called()
        plot(runs, [self.spec], vary=('repeats',))
        self.assertEqual(self.render.call_args.args[0]['values'], {'a': [[1.0]], 'b': [[2.0]]})

    def test_runs_of_different_sources_never_pool(self):
        runs = [self.make_run('r1', 'a', 1.0), self.make_run('r2', 'b', 2.0, source='t')]
        for vary in ((), ('repeats',)):
            with self.subTest(vary=vary), self.assertRaisesRegex(AssertionError, 'source_sha256'):
                plot(runs, [self.spec], vary=vary)
        self.render.assert_not_called()

    def test_unknown_target_names_fail_instead_of_drawing_nothing(self):
        run = self.make_run('r1', 'nfs', 1.0)
        with mock.patch('agent_fs_bench.analysis.workload', return_value=fake_workload()), \
                self.assertRaisesRegex(AssertionError, r'--targets: .*measured \[.Ours'):
            plot([run], targets=['Ours', 'nfs'])
        self.render.assert_not_called()

    def test_custom_figure_without_rows_fails(self):
        run = self.make_run('r1', 'a', 1.0)
        with self.assertRaisesRegex(AssertionError, 'rows matching'):
            plot([run], [Figure('phases', 'procs', 'ops_per_s', where={'procs': 8})])


class SourceDigestTests(unittest.TestCase):
    def test_config_literal_is_excluded_but_any_other_code_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workload.py'

            def digest(clients, code):
                path.write_text(f"CONFIG: dict = {{\n    'clients': {clients},\n}}\n"
                                f"def run():\n    return {code}\n")
                return _without_config(path)

            self.assertEqual(digest(64, 1), digest(256, 1))
            self.assertNotEqual(digest(64, 1), digest(64, 2))


if __name__ == '__main__':
    unittest.main()
