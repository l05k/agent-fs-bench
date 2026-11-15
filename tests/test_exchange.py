import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_fs_bench.analysis import Figure, figure_data, load_snapshots
from agent_fs_bench.exchange import export_packet, import_packet
from agent_fs_bench.replay_report import summarize
from agent_fs_bench.results import read_table


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / 'reviewed'
        self.stage.mkdir()
        (self.stage / 'plan.md').write_text('# Plan\n\n```sh\necho commands are never executed\n```\n')
        (self.stage / 'execution.md').write_text('# Actual execution\nNo observations were omitted.\n')
        self.packet = self.root / 'reply.md'
        self.destination = self.root / 'imported'

    def inventory(self):
        (self.stage / 'inventory').mkdir()
        (self.stage / 'inventory' / 'issues.csv').write_bytes(
            b'id,status,evidence\r\nE01,confirmed,"same host alias, different cache state"\r\n')
        (self.stage / 'legacy').mkdir()
        (self.stage / 'legacy' / 'old.csv').write_text('task,seconds\nsmall,0.125\n')
        export_packet(self.stage, self.packet, kind='inventory', batch='round-00')

    def replay(self):
        spec = {'benchmark': 'swe', 'agent': 'codex', 'model': 'qwen'}
        samples = {'ext4': {'a': [2, 4], 'b': [10, 10], 'c': [3, 3]},
                   'apsara': {'a': [1, 2], 'b': [20, 20], 'c': [1, None]}}
        for target, tasks in samples.items():
            run_id = f'run-{target}'
            run = self.stage / 'results' / 'agent-replay' / run_id
            run.mkdir(parents=True)
            keys = ['target', 'case', 'benchmark', 'agent', 'task_id', 'tasks', 'sample', 'repeat']
            snapshot = {'benchmark': 'agent-replay', 'run_id': run_id,
                        'git_commit': 'commit', 'source_sha256': 'source',
                        'config': {'tasks': 1, 'repeats': 2}, 'nodes': [],
                        'planned_targets': [target], 'targets': [{'name': target,
                            'info': {'hosts': {'machine-01': {'cases': {
                                case: {**spec, 'task_id': case} for case in tasks}}}}}],
                        'tables': {'outcomes': {'keys': keys + ['status'], 'metrics': ['attempts']},
                                   'replays': {'keys': keys, 'metrics': ['tool_s']}}}
            (run / 'config.json').write_text(json.dumps(snapshot))
            outcomes, times = [], []
            for case, repeats in tasks.items():
                for repeat, seconds in enumerate(repeats, 1):
                    row = dict(zip(keys, [target, case, 'swe', 'codex', case, 1, 0, repeat]))
                    outcomes.append({**row, 'status': 'ok' if seconds is not None else 'divergence',
                                     'attempts': 1})
                    if seconds is not None:
                        times.append({**row, 'tool_s': seconds})
            for table, rows in (('outcomes', outcomes), ('replays', times)):
                with (run / f'{table}.csv').open('w', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=rows[0])
                    writer.writeheader()
                    writer.writerows(rows)
        export_packet(self.stage, self.packet, kind='results', batch='replay-01')

    def test_inventory_keeps_old_data_and_nested_commands_as_text(self):
        self.inventory()
        receipt = import_packet(self.packet, self.destination)
        self.assertEqual(read_table(self.destination / 'legacy' / 'old.csv'),
                         [{'task': 'small', 'seconds': '0.125'}])
        self.assertEqual((self.destination / 'plan.md').read_text(),
                         (self.stage / 'plan.md').read_text())
        self.assertNotIn('\r', (self.destination / 'inventory' / 'issues.csv').read_text())
        self.assertEqual(receipt['packet_sha256'], hashlib.sha256(self.packet.read_bytes()).hexdigest())
        self.assertFalse((self.destination / 'results').exists())

    def test_paired_report_is_unchanged_after_single_document_round_trip(self):
        self.replay()
        before = summarize(sorted((self.stage / 'results' / 'agent-replay').iterdir()), 'ext4')
        import_packet(self.packet, self.destination)
        after = summarize(sorted((self.destination / 'results' / 'agent-replay').iterdir()), 'ext4')
        self.assertEqual(after, before)
        panel, = after['panels']
        self.assertEqual(panel['task_ratios']['apsara'], {'a': 0.5, 'b': 2.0})
        self.assertEqual(panel['outcomes']['apsara']['divergence'], 1)

    def test_partial_run_and_failed_rows_remain_distinguishable(self):
        run = self.stage / 'results' / 'mount-scale' / 'partial'
        run.mkdir(parents=True)
        snapshot = {'benchmark': 'mount-scale', 'run_id': 'partial', 'git_commit': 'commit',
                    'source_sha256': 'source', 'config': {'tasks': 8}, 'nodes': [],
                    'planned_targets': ['apsara', 'nas'], 'targets': [{'name': 'apsara'}],
                    'tables': {}}
        (run / 'config.json').write_text(json.dumps(snapshot))
        # A first failed target can leave a CSV absent from checkpointed schemas.
        (run / 'phases.csv').write_text('target,tasks,ops_per_s\napsara,8,10\nnas,8,999\n')
        export_packet(self.stage, self.packet, kind='results', batch='partial-01')
        import_packet(self.packet, self.destination)
        imported = self.destination / 'results' / 'mount-scale' / 'partial'
        facts, = load_snapshots([imported], ())
        self.assertEqual(facts['planned_targets'], ['apsara', 'nas'])
        completed = [target['name'] for target in facts['targets']]
        accepted = [row for row in read_table(imported / 'phases.csv') if row['target'] in completed]
        _, data = figure_data(Figure('phases', 'tasks', 'ops_per_s'), accepted, completed, (3.5, 1.5))[0]
        self.assertEqual(data['values'], {'apsara': [[10.0]]})

    def test_truncated_changed_or_duplicate_documents_do_not_write_any_files(self):
        self.inventory()
        original = self.packet.read_text()
        variants = [original[:original.rfind('````')],
                    original.replace('small,0.125', 'small,9.125'),
                    original + original]
        for index, text in enumerate(variants):
            document = self.root / f'bad-{index}.md'
            destination = self.root / f'bad-{index}'
            document.write_text(text)
            with self.subTest(index=index), self.assertRaises(ValueError):
                import_packet(document, destination)
            self.assertFalse(destination.exists())

    def test_manifest_cannot_write_outside_destination(self):
        files = {'plan.md': 'plan\n', 'execution.md': 'actual\n',
                 'inventory/../escaped.txt': 'not allowed\n'}
        manifest = {'kind': 'inventory', 'batch': 'unsafe', 'files': [
            {'path': path, 'bytes': len(text.encode()),
             'sha256': hashlib.sha256(text.encode()).hexdigest()} for path, text in sorted(files.items())]}
        text = '```afb-manifest\n' + json.dumps(manifest) + '\n```\n'
        text += ''.join(f'```afb-file {path}\n{body}```\n' for path, body in files.items())
        self.packet.write_text(text)
        with self.assertRaisesRegex(ValueError, 'Unsafe packet path'):
            import_packet(self.packet, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'escaped.txt').exists())

    def test_export_does_not_follow_symlinks_or_silently_drop_logs(self):
        (self.stage / 'inventory').mkdir()
        sensitive = self.root / 'private.txt'
        sensitive.write_text('private')
        link = self.stage / 'inventory' / 'linked.txt'
        link.symlink_to(sensitive)
        with self.assertRaisesRegex(ValueError, 'regular text files'):
            export_packet(self.stage, self.packet, kind='inventory', batch='bad')
        link.unlink()
        (self.stage / 'inventory' / 'raw.log').write_text('unreviewed logs')
        with self.assertRaisesRegex(ValueError, 'Not an exchange artifact'):
            export_packet(self.stage, self.packet, kind='inventory', batch='bad')
        self.assertFalse(self.packet.exists())

    def test_file_directory_collision_is_rejected_before_creating_output(self):
        files = {'plan.md': 'plan\n', 'execution.md': 'actual\n',
                 'inventory/item.txt': 'one\n', 'inventory/item.txt/nested.md': 'two\n'}
        manifest = {'kind': 'inventory', 'batch': 'collision', 'files': [
            {'path': path, 'bytes': len(text.encode()),
             'sha256': hashlib.sha256(text.encode()).hexdigest()} for path, text in sorted(files.items())]}
        self.packet.write_text('```afb-manifest\n' + json.dumps(manifest) + '\n```\n'
                               + ''.join(f'```afb-file {path}\n{body}```\n'
                                         for path, body in files.items()))
        with self.assertRaisesRegex(ValueError, 'also used as a directory'):
            import_packet(self.packet, self.destination)
        self.assertFalse(self.destination.exists())


    def test_missing_declared_table_cannot_be_passed_off_as_a_complete_transfer(self):
        self.replay()
        self.packet.unlink()
        next((self.stage / 'results').glob('*/*/replays.csv')).unlink()
        with self.assertRaisesRegex(ValueError, 'missing declared table'):
            export_packet(self.stage, self.packet, kind='results', batch='incomplete')
        self.assertFalse(self.packet.exists())

    def test_import_never_overwrites_an_existing_directory(self):
        self.inventory()
        self.destination.mkdir()
        existing = self.destination / 'keep.txt'
        existing.write_text('keep')
        with self.assertRaises(FileExistsError):
            import_packet(self.packet, self.destination)
        self.assertEqual(existing.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
