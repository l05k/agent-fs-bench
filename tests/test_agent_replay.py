import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import agent_replay
from agent_fs_bench.benchmarks.agent_replay import (
    Mount, ReplayCase, cases_path, check_consistent, check_storage, fingerprint, load_cases,
    measurements, restore, restore_verified,
)
from agent_fs_bench.results import PROJECT_ROOT

class ReplayInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'initial'
        self.source.mkdir()
        self.task = self.root / 'task'
        self.task.mkdir()
        (self.task / 'task.toml').write_text('version = "1.0"\n')
        self.cassette = self.root / 'cassette'
        self.cassette.mkdir()
        self.config = self.root / 'cases.toml'
        self.config.write_text(
            '[[cases]]\nname = "task-a"\ntask = "task"\ncassette = "cassette"\n'
            '[[cases.mounts]]\nsource = "initial"\ncontainer = "/app"\n'
        )

    def test_unaccepted_cassette_cannot_become_a_performance_sample(self):
        with self.assertRaisesRegex(ValueError, 'validate cassette'):
            load_cases(self.config)
        (self.cassette / 'validation.json').write_text(json.dumps({'minimum_reward': 1.0}))
        case, = load_cases(self.config)
        self.assertEqual(case.mounts[0].source, self.source.resolve())

    def test_overlapping_mounts_cannot_hide_restored_data(self):
        (self.cassette / 'validation.json').write_text('{}')
        with self.config.open('a') as handle:
            handle.write('[[cases.mounts]]\nsource = "initial"\ncontainer = "/app/data"\n')
        with self.assertRaisesRegex(ValueError, 'overlapping container mounts'):
            load_cases(self.config)

    def test_relative_cases_file_resolves_in_this_checkout(self):
        self.assertEqual(cases_path('local/agent-replay.toml'),
                         PROJECT_ROOT / 'local' / 'agent-replay.toml')
        self.assertEqual(cases_path(str(self.config)), self.config)

    def test_inputs_must_stay_outside_the_measured_target(self):
        case = ReplayCase('task-a', self.task, self.cassette,
                          (Mount(self.source, '/app'),))
        target = self.root / 'target'
        target.mkdir()
        check_storage(case, target, self.config)
        inside = target / 'initial'
        inside.mkdir()
        with self.assertRaisesRegex(ValueError, 'must be outside target'):
            check_storage(ReplayCase('task-a', self.task, self.cassette,
                                     (Mount(inside, '/app'),)), target, self.config)

    def test_machines_must_fingerprint_the_same_inputs(self):
        facts = {'replay': {'source_sha256': 'a'}, 'cases': {'task-a': {'model': 'm'}}}
        check_consistent([{'node': 'local', 'facts': facts},
                          {'node': 'node-a', 'facts': facts}])
        other = {'replay': {'source_sha256': 'b'}, 'cases': {'task-a': {'model': 'm'}}}
        with self.assertRaisesRegex(ValueError, 'differ between machines'):
            check_consistent([{'node': 'local', 'facts': facts},
                              {'node': 'node-a', 'facts': other}])

    def test_only_a_measured_replay_yields_measurements(self):
        summary = {'mode': 'replay', 'checked': False, 'measured': True,
                   'elapsed_sec': 12.5, 'model_requests': 3,
                   'phase_seconds': {'environment_setup': 1.5, 'verifier': None}}
        self.assertEqual(measurements(summary, self.root / 'output'),
                         {'elapsed_s': 12.5, 'model_requests': 3,
                          'phases': {'environment_setup': 1.5}})
        for name in ('checked', 'measured'):
            with self.subTest(field=name):
                with self.assertRaisesRegex(ValueError, 'not a measured replay'):
                    measurements({**summary, name: not summary[name]}, self.root / 'output')
        with self.assertRaisesRegex(ValueError, 'not a measured replay'):
            measurements({**summary, 'mode': 'validate'}, self.root / 'output')

    def test_content_modes_and_links_distinguish_experiment_inputs(self):
        payload = self.source / '.hidden'
        payload.write_bytes(b'original')
        payload.chmod(0o644)
        (self.source / 'link').symlink_to('.hidden')
        original = fingerprint(self.source)
        payload.write_bytes(b'modified')
        self.assertNotEqual(fingerprint(self.source), original)
        payload.write_bytes(b'original')
        payload.chmod(0o755)
        self.assertNotEqual(fingerprint(self.source), original)
        payload.chmod(0o644)
        (self.source / 'link').unlink()
        (self.source / 'link').symlink_to('missing')
        self.assertNotEqual(fingerprint(self.source), original)

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('cp'), 'GNU cp on Linux required')
    def test_each_restore_preserves_tree_semantics_without_reusing_mutations(self):
        payload = self.source / '.hidden'
        payload.write_bytes(b'initial')
        payload.chmod(0o755)
        os.link(payload, self.source / 'hardlink')
        (self.source / 'symlink').symlink_to('.hidden')
        (self.source / 'empty').mkdir()
        first, second = self.root / 'first', self.root / 'second'
        expected = fingerprint(self.source)
        restore(self.source, first)
        self.assertEqual(fingerprint(first), expected)
        (first / '.hidden').write_bytes(b'changed by task')
        (first / 'created-by-task').touch()
        restore(self.source, second)
        self.assertEqual(fingerprint(second), expected)
        self.assertEqual((first / 'hardlink').read_bytes(), b'changed by task')
        self.assertEqual((second / '.hidden').read_bytes(), b'initial')
        self.assertFalse((second / 'created-by-task').exists())
        self.assertEqual(payload.read_bytes(), b'initial')

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('cp'), 'GNU cp on Linux required')
    def test_restored_metadata_loss_blocks_the_measurement(self):
        payload = self.source / 'payload'
        payload.write_bytes(b'initial')
        payload.chmod(0o755)
        os.link(payload, self.source / 'hardlink')
        expected = fingerprint(self.source)
        destination = self.root / 'restored'
        restore_verified(self.source, destination, expected)

        def restore_with_loss(source, destination):
            restore(source, destination)
            (destination / 'payload').chmod(0o644)

        with (
            mock.patch.object(agent_replay, 'restore', side_effect=restore_with_loss),
            self.assertRaisesRegex(RuntimeError, 'restored task data differs from source'),
        ):
            restore_verified(self.source, self.root / 'lossy', expected)

if __name__ == '__main__':
    unittest.main()
