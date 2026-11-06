import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from agent_fs_bench.benchmarks.agent_replay import fingerprint, load_cases, restore


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


if __name__ == '__main__':
    unittest.main()
