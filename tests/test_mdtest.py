import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_fs_bench.benchmarks import mdtest, mdtest_peak
from agent_fs_bench.benchmarks.mdtest import (
    BenchmarkError,
    Case,
    check_banner,
    check_workload,
    read_phases,
    unexpected_warnings,
)

PHASE_NAMES = (
    'Directory creation', 'Directory stat', 'Directory read', 'Directory rename',
    'Directory removal', 'File creation', 'File stat', 'File read',
    'File removal', 'Tree creation', 'Tree removal',
)

class CsvTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.csv = Path(temporary.name) / 'mdtest.csv'

    def write(self, aggregate, ops=128):
        """Write an aggregate row between rank rows with different timings."""
        header = ['rank', 'items']
        for name in PHASE_NAMES:
            header += [f'rate-{name}', f'time-{name}']

        def row(rank, values, names):
            cells = [rank, str(ops // 2) if rank == 'all' else '']
            for name in names:
                rate, elapsed = values.get(name, ('', ''))
                cells += [rate, elapsed]
            return cells

        values = {
            name: (f'{ops / 0.5:.10e}', f'{0.5:.10e}') for name in PHASE_NAMES
        }
        values.update(aggregate)
        # Per-rank rows carry pre-barrier times for the nine per-rank phases
        # and must never be read.
        rank_values = {name: ('9.9000000000e+01', '9.9000000000e+00')
                       for name in PHASE_NAMES}
        lines = [header, row('0', rank_values, PHASE_NAMES[:9]),
                 row('all', values, PHASE_NAMES), row('1', rank_values, PHASE_NAMES[:9])]
        self.csv.write_text(
            '\n'.join(','.join(cells) for cells in lines) + '\n',
            encoding='utf-8',
        )

    def test_aggregate_row_is_read_instead_of_per_rank_rows(self):
        self.write({'File stat': (f'{128 / 2:.10e}', f'{2:.10e}')})
        phases = read_phases(self.csv, 'file', 128)
        self.assertEqual(phases, [
            ('file-create', 128, 0.5, 256.0),
            ('file-stat', 128, 2.0, 64.0),
            ('file-open', 128, 0.5, 256.0),
            ('file-unlink', 128, 0.5, 256.0),
        ])

    def test_missing_aggregate_row_is_rejected(self):
        self.write({})
        rows = self.csv.read_text(encoding='utf-8').splitlines()
        self.csv.write_text('\n'.join(row for row in rows if not row.startswith('all,')) + '\n',
                            encoding='utf-8')
        with self.assertRaisesRegex(BenchmarkError, "aggregate 'all' row"):
            read_phases(self.csv, 'file', 128)

    def test_non_finite_or_non_positive_phase_time_is_rejected(self):
        for value in ('inf', 'nan', '0', '-1'):
            with self.subTest(elapsed=value):
                self.write({'File read': ('256', value)})
                with self.assertRaises(BenchmarkError):
                    read_phases(self.csv, 'file', 128)

    def test_phase_that_did_not_run_is_rejected(self):
        self.write({'File stat': ('', '')})
        with self.assertRaisesRegex(BenchmarkError, 'File stat phase did not run'):
            read_phases(self.csv, 'file', 128)

    def test_directory_rates_use_total_work_despite_corrupted_upstream_csv_rates(self):
        # 4.0.0 aggregate create/stat counts are overwritten with rank-local
        # counts. Directory read is not an operation and can contain inf.
        self.write({'Directory creation': ('128', '0.5'),
                    'Directory stat': ('32', '2.0'),
                    'Directory read': ('inf', '0')})
        phases = read_phases(self.csv, 'dir', 128)
        self.assertEqual(phases[0], ('dir-mkdir', 128, 0.5, 256.0))
        self.assertEqual(phases[1], ('dir-stat', 128, 2.0, 64.0))

class WarningTests(unittest.TestCase):
    def test_only_the_zero_byte_read_notice_is_expected(self):
        log = (
            'WARNING: Read bytes is 0, thus, a read test will actually just open/close\n'
            'WARNING: unable to stat file mdtest.shared.7\n'
        )
        self.assertEqual(
            unexpected_warnings(log),
            ['unable to stat file mdtest.shared.7'],
        )
        self.assertEqual(
            unexpected_warnings('WARNING: Read bytes is 0, thus, a read test '
                                'will actually just open/close\n'),
            [],
        )

class BannerTests(unittest.TestCase):
    log = Path('/results/file-private-P4-rep1.log')

    def banner(self, version='4.0.0', tasks=4, nodes=1):
        return (f"mdtest-{version} was launched with {tasks} total task(s) "
                f"on {nodes} node(s)\n")

    def test_matching_launch_is_accepted(self):
        check_banner(self.banner(), 4, self.log)


    def test_singleton_launch_is_rejected(self):
        with self.assertRaisesRegex(BenchmarkError, 'expected 4 task'):
            check_banner(self.banner(tasks=1), 4, self.log)

    def test_multiple_nodes_are_rejected(self):
        with self.assertRaisesRegex(BenchmarkError, 'expected 4 task'):
            check_banner(self.banner(nodes=2), 4, self.log)

    def test_unpinned_version_is_rejected(self):
        with self.assertRaisesRegex(BenchmarkError, 'reported version 3.5.0'):
            check_banner(self.banner(version='3.5.0'), 4, self.log)

class WorkloadTests(unittest.TestCase):
    log = Path('/results/mdtest.log')

    def test_matching_file_and_directory_workloads_are_accepted(self):
        for subset, stdout in (
            ('file', 'Nodemap: 1111\n4 tasks, 128 files\n\nSUMMARY rate:\n'),
            ('dir', 'Nodemap: 1111\n4 tasks, 128 directories\n\nSUMMARY rate:\n'),
        ):
            with self.subTest(subset=subset):
                check_workload(stdout, Case(subset, 'private', 4, 1), 128, self.log)

    def test_wrong_process_count_total_or_object_type_is_rejected(self):
        for report in ('1 tasks, 128 files', '4 tasks, 32 files',
                       '4 tasks, 128 directories', ''):
            with self.subTest(report=report):
                with self.assertRaises(BenchmarkError):
                    check_workload(report, Case('file', 'private', 4, 1), 128, self.log)


class ParseArgsTests(unittest.TestCase):
    """A typo or uneven split must fail before measuring, not run under a wrong label."""

    def test_unknown_layout_subset_or_uneven_split_is_rejected(self):
        for override in ({'layouts': ('Private',)}, {'subsets': ('files',)},
                         {'conc_levels': (3,)}, {'total_items': 4, 'conc_levels': (4,)}):
            with self.subTest(override=override), mock.patch.dict(mdtest.CONFIG, override), \
                    self.assertRaises(BenchmarkError):
                mdtest.parse_args([])

    def test_peak_rejects_unknown_layout(self):
        with mock.patch.dict(mdtest_peak.CONFIG, {'layouts': ('Private',)}), \
                self.assertRaises(BenchmarkError):
            mdtest_peak.parse_args([])


if __name__ == '__main__':
    unittest.main()
