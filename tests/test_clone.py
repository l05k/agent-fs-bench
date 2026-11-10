import argparse
import shutil
import tempfile
import unittest
from pathlib import Path

import agent_fs_bench.benchmarks.clone as clone
from agent_fs_bench.benchmarks.clone import (
    BenchmarkError,
    Copier,
    Workload,
    build_plan,
    gen_dataset,
    prepare_destination,
    tree_inventory,
    validate_copier,
    verify_tree,
)

CP_R = Copier('cp-r', ('cp', '-r', '{src}', '{dst}'))
CP_RL = Copier('cp-rl', ('cp', '-rl', '{src}', '{dst}'))

class TreeVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def tree(self, name, payload, *, empty=True):
        root = self.root / name
        root.mkdir()
        (root / 'payload.bin').write_bytes(payload)
        if empty:
            (root / 'empty').mkdir()
        return root

    def test_same_size_wrong_contents_rejected_when_requested(self):
        source = self.tree('source', b'A' * 64)
        destination = self.tree('destination', b'B' * 64)
        verify_tree(source, destination)  # inventory and sizes match
        with self.assertRaises(BenchmarkError):
            verify_tree(source, destination, contents=True)

    def test_missing_empty_directory_is_rejected(self):
        source = self.tree('source', b'payload')
        destination = self.tree('destination', b'payload', empty=False)
        with self.assertRaises(BenchmarkError):
            verify_tree(source, destination)

@unittest.skipUnless(shutil.which('cp'), 'cp is required for copier tests')
class CopierValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()

    def test_real_recursive_copy_passes_and_cleans_up(self):
        validate_copier(CP_R, self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_hardlinked_tree_is_rejected_and_cleans_up(self):
        with self.assertRaises(BenchmarkError):
            validate_copier(CP_RL, self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])

class WorkloadPlanTests(unittest.TestCase):
    def test_smoke_shrinks_shape_and_repeats_but_not_tasks(self):
        args = clone.parse_args(['--smoke'])
        self.assertEqual(build_plan(args), Workload(32, 4096))
        self.assertEqual(args.repeats, 2)
        self.assertEqual(args.tasks, clone.CONFIG['tasks'])

    def test_non_positive_dimensions_are_rejected_and_tasks_is_left_to_allocation(self):
        for name in ('files', 'file_size_bytes', 'files_per_dir', 'repeats'):
            args = argparse.Namespace(**{**clone.CONFIG, name: 0})
            with self.subTest(name=name), self.assertRaisesRegex(BenchmarkError, name):
                build_plan(args)
        build_plan(argparse.Namespace(**{**clone.CONFIG, 'tasks': None}))

    def test_copier_selection_fails_instead_of_measuring_something_else(self):
        commands = {'cp-r': 'cp -r {src} {dst}'}
        for selection, commands, message in (
                ({'copier': 'cp-r', 'target_copiers': {'fs': 'lakebase'}}, commands,
                 'no command configured'),
                ({'copier': 'cp-r', 'target_copiers': {}},
                 {'cp-r': 'not-an-installed-command-xyz {src} {dst}'}, 'executable not found')):
            with self.subTest(message=message), self.assertRaisesRegex(BenchmarkError, message):
                clone.resolve_copier(argparse.Namespace(**selection, commands=commands), 'fs')

class DatasetShapeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_dataset_groups_files_and_includes_an_empty_directory(self):
        source = self.root / 'source'
        gen_dataset(Workload(7, 64), source, files_per_dir=3)
        inventory = tree_inventory(source)

        files = {key: size for key, size in inventory.items() if size is not None}
        self.assertEqual(sorted(files.values()), [64] * 7)
        self.assertEqual({Path(key).parent.name for key in files}, {'d_0', 'd_1', 'd_2'})
        self.assertIsNone(inventory['empty'])
        self.assertEqual(
            sum(1 for size in inventory.values() if size is None),
            5,  # '.', three populated data directories, one empty directory
        )

class DestinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.task_dir = Path(temporary.name)

    def test_each_batch_starts_from_an_absent_destination(self):
        stale = self.task_dir / 'destination' / 'tree'
        stale.mkdir(parents=True)
        (stale / 'old.bin').write_bytes(b'old')

        destination = prepare_destination(self.task_dir)

        self.assertEqual(destination, self.task_dir / 'destination' / 'tree')
        self.assertFalse(destination.exists())
        self.assertEqual(list((self.task_dir / 'destination').iterdir()), [])

if __name__ == '__main__':
    unittest.main()
