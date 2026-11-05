import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from agent_fs_bench.analysis import SUMMARY_SQL, clone_plot_data, connect
from agent_fs_bench.results import RunContext
from agent_fs_bench.targets import Target

MIB = 1024 * 1024


class ClonePlotDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = io.StringIO()

    def emit(self, metrics, details, *, target='ext4', case='bytes/N4_S262144/C1',
             repeat=1, benchmark='clone', parameters=None, source='source'):
        context = RunContext(benchmark, 'run-a', Target(target, Path('/mnt') / target),
                             Path('/missing/work'), Path('/missing/results'),
                             parameters or {}, source, self.output)
        context.emit(case, metrics, repeat=repeat, details=details)

    def clone_metrics(self, total_bytes, total_files, concurrency, metric, value):
        return {
            'total_bytes': total_bytes, 'total_files': total_files,
            'concurrency': concurrency, 'wall_s': 1.0, 'clones_s': 1.0,
            metric: value,
        }

    def requests(self, metric, values):
        return [{'copy_s': 0.5, 'fsync_s': 0.1, metric: value} for value in values]

    def clone_args(self, metric, **options):
        values = {'metric': metric, 'cases': None, 'targets': None,
                  'ylabel': None, 'size': (3.5, 1.5)}
        values.update(options)
        return SimpleNamespace(**values)

    def connection(self):
        path = self.root / 'rows.jsonl'
        path.write_text(self.output.getvalue(), encoding='utf-8')
        connection = connect([str(path)])
        self.addCleanup(connection.close)
        return connection

    def test_summary_keeps_copiers_as_separate_series(self):
        details = {'experiment': 'bytes', 'kind': 'large', 'fstype': 'ext4',
                   'requests': self.requests('latency_s', [1.0])}
        self.emit({'latency_s': 1.0},
                  {**details, 'copier': 'cp-r'})
        self.emit({'latency_s': 3.0},
                  {**details, 'copier': 'cp-r'}, repeat=2)
        self.emit({'latency_s': 4.0},
                  {**details, 'copier': 'cp-no-reflink'})
        self.emit({'latency_ms': 9.0}, {}, benchmark='example', case='write')

        rows = self.connection().execute(SUMMARY_SQL).fetchall()
        observed = {(row[9], row[0], row[1], row[2]): (row[3], row[4]) for row in rows}
        self.assertEqual(observed, {
            ('clone', 'ext4/cp-r', 'bytes/N4_S262144/C1', 'latency_s'): (2, 2.0),
            ('clone', 'ext4/cp-no-reflink', 'bytes/N4_S262144/C1', 'latency_s'): (1, 4.0),
            ('example', 'ext4', 'write', 'latency_ms'): (1, 9.0),
        })

    def test_latency_pools_requests_while_other_metrics_keep_repeat_values(self):
        for repeat, values in ((1, [1.0, 3.0]), (2, [5.0, 7.0])):
            self.emit(
                {**self.clone_metrics(MIB, 4, 2, 'latency_s', sum(values) / len(values)),
                 'clones_s': 1.0 + repeat},
                {'experiment': 'bytes', 'kind': 'large', 'copier': 'cp-r',
                 'fstype': 'ext4', 'requests': self.requests('latency_s', values)},
                repeat=repeat)

        connection = self.connection()
        latency = clone_plot_data(connection, self.clone_args('latency_s'))
        self.assertEqual([name for name, _ in latency], ['bytes'])
        data = latency[0][1]
        self.assertEqual(data['points']['ext4/cp-r'],
                         [{'x': 1.0, 'values': [1.0, 3.0, 5.0, 7.0]}])
        self.assertTrue(data['logx'])

        clones = clone_plot_data(connection, self.clone_args('clones_s'))
        self.assertEqual(clones[0][1]['points']['ext4/cp-r'],
                         [{'x': 1.0, 'values': [2.0, 3.0]}])

    def test_copiers_compare_as_separate_series_on_the_same_x(self):
        for copier, latency in (('cp-r', 1.0), ('cp-no-reflink', 4.0)):
            self.emit(
                self.clone_metrics(MIB, 4, 1, 'latency_s', latency),
                {'experiment': 'bytes', 'kind': 'large', 'copier': copier,
                 'fstype': 'ext4', 'requests': self.requests('latency_s', [latency])})

        figures = clone_plot_data(self.connection(), self.clone_args(
            'latency_s', targets=['ext4/cp-r', 'ext4/cp-no-reflink']))
        self.assertEqual([name for name, _ in figures], ['bytes'])
        data = figures[0][1]
        self.assertEqual(data['series'], ['ext4/cp-r', 'ext4/cp-no-reflink'])
        self.assertEqual(data['points'], {
            'ext4/cp-r': [{'x': 1.0, 'values': [1.0]}],
            'ext4/cp-no-reflink': [{'x': 1.0, 'values': [4.0]}],
        })

    def test_sweeps_split_by_experiment_and_kind_and_honor_case_filter(self):
        workloads = (
            ('bytes/N4_S262144/C1', MIB, 4, 1, 'bytes', 'large'),
            ('bytes/N4_S1048576/C1', 4 * MIB, 4, 1, 'bytes', 'large'),
            ('files/N8_S4096/C1', 8 * 4096, 8, 1, 'files', 'small'),
            ('files/N32_S4096/C1', 32 * 4096, 32, 1, 'files', 'small'),
            ('concurrency/N4_S1048576/C1', 4 * MIB, 4, 1, 'concurrency', 'large'),
            ('concurrency/N4_S1048576/C4', 4 * MIB, 4, 4, 'concurrency', 'large'),
            ('concurrency/N32_S4096/C1', 32 * 4096, 32, 1, 'concurrency', 'small'),
            ('concurrency/N32_S4096/C4', 32 * 4096, 32, 4, 'concurrency', 'small'),
        )
        for case, total_bytes, total_files, concurrency, experiment, kind in workloads:
            self.emit(
                self.clone_metrics(total_bytes, total_files, concurrency, 'clones_s', 2.0),
                {'experiment': experiment, 'kind': kind, 'copier': 'cp-r',
                 'fstype': 'ext4', 'requests': [
                     {'copy_s': 0.5, 'fsync_s': 0.1, 'latency_s': 0.6}
                     for _ in range(concurrency)]},
                case=case)

        connection = self.connection()
        figures = dict(clone_plot_data(connection, self.clone_args('clones_s')))
        self.assertEqual(list(figures), ['bytes', 'files',
                                         'concurrency-large', 'concurrency-small'])
        x_values = {name: [point['x'] for point in data['points']['ext4/cp-r']]
                    for name, data in figures.items()}
        self.assertEqual(x_values, {
            'bytes': [1.0, 4.0], 'files': [8, 32],
            'concurrency-large': [1, 4], 'concurrency-small': [1, 4],
        })
        self.assertTrue(figures['bytes']['logx'])
        self.assertTrue(figures['files']['logx'])
        self.assertFalse(figures['concurrency-large']['logx'])
        self.assertFalse(figures['concurrency-small']['logx'])

        filtered = clone_plot_data(
            connection, self.clone_args('clones_s', cases=['bytes/N4_S262144/C1']))
        self.assertEqual([name for name, _ in filtered], ['bytes'])
        self.assertEqual(filtered[0][1]['points']['ext4/cp-r'],
                         [{'x': 1.0, 'values': [2.0]}])

    def test_missing_x_values_across_series_fail_instead_of_plotting_gaps(self):
        details = {'experiment': 'bytes', 'kind': 'large', 'fstype': 'ext4',
                   'requests': self.requests('latency_s', [1.0])}
        for total_bytes in (MIB, 4 * MIB):
            self.emit(self.clone_metrics(total_bytes, 4, 1, 'latency_s', 1.0),
                      {**details, 'copier': 'cp-r'})
        self.emit(self.clone_metrics(MIB, 4, 1, 'latency_s', 2.0),
                  {**details, 'copier': 'cp-no-reflink'})

        with self.assertRaisesRegex(ValueError, 'same x values'):
            clone_plot_data(self.connection(), self.clone_args(
                'latency_s', targets=['ext4/cp-r', 'ext4/cp-no-reflink']))

    def test_incompatible_payloads_within_a_sweep_fail(self):
        details = {'experiment': 'bytes', 'kind': 'large', 'fstype': 'ext4',
                   'requests': self.requests('latency_s', [1.0])}
        self.emit(self.clone_metrics(MIB, 4, 1, 'latency_s', 1.0),
                  {**details, 'copier': 'cp-r'}, case='bytes/N4_S262144/C1')
        self.emit(self.clone_metrics(4 * MIB, 8, 1, 'latency_s', 1.0),
                  {**details, 'copier': 'cp-r'}, case='bytes/N8_S524288/C1')

        with self.assertRaisesRegex(ValueError, 'incompatible total file counts'):
            clone_plot_data(self.connection(), self.clone_args('latency_s'))

        self.output = io.StringIO()
        for concurrency, total_bytes in ((1, 4 * MIB), (4, 8 * MIB)):
            self.emit(
                self.clone_metrics(total_bytes, 4, concurrency, 'latency_s', 1.0),
                {'experiment': 'concurrency', 'kind': 'large', 'copier': 'cp-r',
                 'fstype': 'ext4', 'requests': self.requests('latency_s', [1.0])},
                case=f'concurrency/N4_S1048576/C{concurrency}')
        with self.assertRaisesRegex(ValueError, 'incompatible payload sizes'):
            clone_plot_data(self.connection(), self.clone_args('latency_s'))

    def test_sweep_mixing_parameter_or_source_identity_fails(self):
        details = {'experiment': 'bytes', 'kind': 'large', 'fstype': 'ext4',
                   'requests': self.requests('latency_s', [1.0])}
        self.emit(self.clone_metrics(MIB, 4, 1, 'latency_s', 1.0),
                  {**details, 'copier': 'cp-r'}, case='bytes/N4_S262144/C1',
                  parameters={'repeats': 5}, source='source-a')
        self.emit(self.clone_metrics(4 * MIB, 4, 1, 'latency_s', 2.0),
                  {**details, 'copier': 'cp-r'}, case='bytes/N4_S1048576/C1',
                  parameters={'repeats': 10}, source='source-a')

        with self.assertRaisesRegex(ValueError,
                                    'bytes sweep mixes 2 configurations') as caught:
            clone_plot_data(self.connection(), self.clone_args('latency_s'))
        message = str(caught.exception)
        self.assertIn('--parameters', message)
        self.assertIn('--source-sha256', message)
        self.assertIn('"repeats":10', message)

    def test_identity_selectors_choose_one_configuration(self):
        details = {'experiment': 'bytes', 'kind': 'large', 'fstype': 'ext4',
                   'requests': self.requests('latency_s', [1.0])}
        for repeats, total_bytes, source, latency in (
                (5, MIB, 'source-a', 1.0), (10, 4 * MIB, 'source-b', 2.0)):
            self.emit(self.clone_metrics(total_bytes, 4, 1, 'latency_s', latency),
                      {**details, 'copier': 'cp-r'},
                      case=f'bytes/N4_S{total_bytes // 4}/C1',
                      parameters={'repeats': repeats}, source=source)

        connection = self.connection()
        selected = clone_plot_data(connection, self.clone_args(
            'latency_s', parameters='{"repeats":5}', source_sha256=['source-a']))
        self.assertEqual(selected,
                         [('bytes', {'series': ['ext4/cp-r'],
                                     'points': {'ext4/cp-r': [{'x': 1.0, 'values': [1.0]}]},
                                     'xlabel': 'Total bytes (MiB)',
                                     'ylabel': 'Clone latency (s)',
                                     'logx': True, 'logy': True, 'size': [3.5, 1.5]})])
        with self.assertRaisesRegex(ValueError, 'no clone measurements match'):
            clone_plot_data(connection, self.clone_args(
                'latency_s', parameters='{"repeats":5}', source_sha256=['source-b']))


if __name__ == '__main__':
    unittest.main()
