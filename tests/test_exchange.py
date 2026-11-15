import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_fs_bench.exchange import export_packet, import_packet


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / 'reviewed'
        self.stage.mkdir()
        (self.stage / 'plan.md').write_text('# Plan\n\n```sh\necho not executed\n```\n')
        (self.stage / 'execution.md').write_text('# Execution\nAll planned ranges are in the summary.\n')
        self.packet = self.root / 'reply.md'
        self.destination = self.root / 'imported'
        (self.stage / 'summary').mkdir()
        (self.stage / 'summary' / 'failures.csv').write_text(
            'group,series,label,count\nreplay,Apsara,timeout,1\nreplay,NAS,divergence,2\n')
        self.write_group('replay', [
            ('Apsara', 'all', 'paired_tool_time_ratio_to_ext4', 0.62, 0.55, 0.80, 88),
            ('NAS', 'all', 'paired_tool_time_ratio_to_ext4', 2.40, 1.90, 3.10, 88),
        ])

    def write_group(self, group, rows, **overrides):
        with (self.stage / 'summary' / f'{group}.csv').open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['series', 'x', 'metric', 'median', 'q1', 'q3', 'n'])
            writer.writerows(rows)
        meta = {'xlabel': 'Backend', 'ylabel': 'Tool time / ext4', 'unit': 'ratio',
                'logx': False, 'logy': False, 'series_order': list(dict.fromkeys(
                    row[0] for row in rows)),
                'source_runs': ['run-a', 'run-b'],
                'formula': 'per-task median over repeats, ratio to ext4, median over tasks'}
        meta.update(overrides)
        (self.stage / 'summary' / f'{group}.json').write_text(json.dumps(meta))

    def test_round_trip_restores_verified_summaries_and_receipt(self):
        result = export_packet(self.stage, self.packet, batch='results-20261115-01')
        self.assertGreater(result['document_bytes'], 0)
        receipt = import_packet(self.packet, self.destination)
        self.assertEqual(receipt['packet_sha256'], hashlib.sha256(self.packet.read_bytes()).hexdigest())
        def normalized(path):
            text = (self.stage / path).read_text().replace('\r\n', '\n')
            return (text + '\n' if not text.endswith('\n') else text).encode()

        self.assertEqual((self.destination / 'summary' / 'replay.csv').read_bytes(),
                         normalized('summary/replay.csv'))
        expected = [{'path': path, 'bytes': len(normalized(path)),
                     'sha256': hashlib.sha256(normalized(path)).hexdigest()}
                    for path in sorted(('execution.md', 'plan.md', 'summary/failures.csv',
                                        'summary/replay.csv', 'summary/replay.json'))]
        self.assertEqual(json.loads((self.destination / 'exchange.json').read_text())['files'], expected)

    def test_entirely_failed_batch_has_no_fabricated_points(self):
        (self.stage / 'summary' / 'replay.csv').unlink()
        (self.stage / 'summary' / 'replay.json').unlink()
        ledger = self.stage / 'summary' / 'failures.csv'
        export_packet(self.stage, self.packet, batch='no-successes')
        import_packet(self.packet, self.destination)
        self.assertEqual(list((self.destination / 'summary').iterdir()),
                         [self.destination / 'summary' / 'failures.csv'])
        self.assertEqual((self.destination / 'summary' / 'failures.csv').read_bytes(),
                         ledger.read_bytes())

    def test_failed_series_without_a_numeric_row_survives_round_trip(self):
        self.write_group('replay', [('Apsara', 'all', 'paired_tool_time_ratio_to_ext4',
                                    0.62, 0.55, 0.80, 88)])
        export_packet(self.stage, self.packet, batch='partial-success')
        import_packet(self.packet, self.destination)
        self.assertIn('replay,NAS,divergence,2\n',
                      (self.destination / 'summary' / 'failures.csv').read_text())

    def test_single_observation_cannot_claim_a_nonzero_iqr(self):
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 0.9, 1.1, 1)])
        with self.assertRaisesRegex(ValueError, 'n=1 requires'):
            export_packet(self.stage, self.packet, batch='invented-spread')
        self.assertFalse(self.packet.exists())

    def test_raw_per_sample_results_are_not_an_exchange_artifact(self):
        raw = self.stage / 'results' / 'mdtest' / 'run-a'
        raw.mkdir(parents=True)
        (raw / 'phases.csv').write_text('target,tasks,ops_per_s\nApsara,1,100\n')
        with self.assertRaisesRegex(ValueError, 'Raw results stay on the test machine'):
            export_packet(self.stage, self.packet, batch='raw')
        self.assertFalse(self.packet.exists())

    def test_missing_metadata_or_invalid_quantiles_do_not_export(self):
        (self.stage / 'summary' / 'replay.json').unlink()
        with self.assertRaisesRegex(ValueError, 'needs both .csv and .json'):
            export_packet(self.stage, self.packet, batch='no-meta')
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 1.2, 0.9, 3)])
        with self.assertRaisesRegex(ValueError, 'q1 <= median <= q3'):
            export_packet(self.stage, self.packet, batch='bad-order')
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 1.0, 1.0, 0)])
        with self.assertRaisesRegex(ValueError, 'positive canonical integer'):
            export_packet(self.stage, self.packet, batch='bad-n')
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 1.0, 1.0, 2),
                                    ('Apsara', 'all', 'x', 2.0, 2.0, 2.0, 2)])
        with self.assertRaisesRegex(ValueError, 'duplicate series/x/metric'):
            export_packet(self.stage, self.packet, batch='dup')
        self.assertFalse(self.packet.exists())

    def test_metadata_must_match_series_and_every_required_field(self):
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 1.0, 1.0, 2)], series_order=['NAS'])
        with self.assertRaisesRegex(ValueError, 'first-appearance order'):
            export_packet(self.stage, self.packet, batch='order')
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 1.0, 1.0, 2)], source_runs=[])
        with self.assertRaisesRegex(ValueError, 'source_runs must be nonempty'):
            export_packet(self.stage, self.packet, batch='no-source')
        self.write_group('replay', [('Apsara', 'all', 'x', 1.0, 1.0, 1.0, 2)], extra='unverified')
        with self.assertRaisesRegex(ValueError, 'metadata keys'):
            export_packet(self.stage, self.packet, batch='extra')
        self.assertFalse(self.packet.exists())

    def test_tampered_truncated_or_duplicated_documents_write_nothing(self):
        export_packet(self.stage, self.packet, batch='tamper')
        original = self.packet.read_text()
        variants = [original.replace('0.62', '0.52'),
                    original[:original.rfind('````')],
                    original + original]
        for index, text in enumerate(variants):
            document, destination = self.root / f'bad-{index}.md', self.root / f'bad-{index}'
            document.write_text(text)
            with self.subTest(index=index), self.assertRaises(ValueError):
                import_packet(document, destination)
            self.assertFalse(destination.exists())

    def test_unsafe_or_colliding_paths_are_rejected_before_writing(self):
        files = {'plan.md': 'plan\n', 'execution.md': 'actual\n',
                 'summary/../escaped.csv': 'series,x,metric,median,q1,q3,n\n'}
        manifest = {'kind': 'summary', 'batch': 'unsafe', 'files': [
            {'path': path, 'bytes': len(text.encode()),
             'sha256': hashlib.sha256(text.encode()).hexdigest()} for path, text in sorted(files.items())]}
        self.packet.write_text('```afb-manifest\n' + json.dumps(manifest) + '\n```\n'
                               + ''.join(f'```afb-file {path}\n{body}```\n'
                                         for path, body in files.items()))
        with self.assertRaisesRegex(ValueError, 'Unsafe packet path'):
            import_packet(self.packet, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'escaped.csv').exists())

    def test_existing_directories_and_links_are_never_overwritten(self):
        self.destination.mkdir()
        (self.destination / 'keep.txt').write_text('keep')
        with self.assertRaises(FileExistsError):
            import_packet(self.packet, self.destination)
        self.assertEqual((self.destination / 'keep.txt').read_text(), 'keep')
        export_packet(self.stage, self.packet, batch='existing')
        with self.assertRaises(FileExistsError):
            export_packet(self.stage, self.packet, batch='again')
        self.packet.unlink()
        self.packet.symlink_to(self.stage / 'plan.md')
        with self.assertRaises(FileExistsError):
            export_packet(self.stage, self.packet, batch='symlink')

    def test_logs_symlinks_and_other_raw_files_are_rejected(self):
        (self.stage / 'summary' / 'stdout.log').write_text('unreviewed log')
        with self.assertRaisesRegex(ValueError, 'Only plan.md, execution.md'):
            export_packet(self.stage, self.packet, batch='log')
        (self.stage / 'summary' / 'stdout.log').unlink()
        secret = self.root / 'secret.txt'
        secret.write_text('secret')
        (self.stage / 'summary' / 'linked.csv').symlink_to(secret)
        with self.assertRaisesRegex(ValueError, 'regular text files'):
            export_packet(self.stage, self.packet, batch='link')
        self.assertFalse(self.packet.exists())

    def test_unregistered_staging_content_is_rejected(self):
        (self.stage / 'summary' / 'orphan.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Undeclared exchange artifacts'):
            export_packet(self.stage, self.packet, batch='orphan')
        (self.stage / 'summary' / 'orphan.json').unlink()
        (self.stage / 'legacy').mkdir()
        with self.assertRaisesRegex(ValueError, 'Unexpected staging directory'):
            export_packet(self.stage, self.packet, batch='directory')
        self.assertFalse(self.packet.exists())

    def test_empty_or_unregistered_series_are_rejected(self):
        self.write_group('replay', [('Apsara', '', 'x', 1.0, 1.0, 1.0, 2)])
        with self.assertRaisesRegex(ValueError, 'empty series, x or metric'):
            export_packet(self.stage, self.packet, batch='empty')
        (self.stage / 'summary' / 'replay.csv').write_text(
            'group,series,x,median\nx,Apsara,all,1\n')
        with self.assertRaisesRegex(ValueError, 'columns must be'):
            export_packet(self.stage, self.packet, batch='bad-header')
        self.assertFalse(self.packet.exists())


if __name__ == '__main__':
    unittest.main()
