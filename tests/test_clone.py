import argparse
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_fs_bench.benchmarks.clone import (
    BenchmarkError,
    Copier,
    Workload,
    build_plan,
    copy_requests,
    gen_dataset,
    prepare_destinations,
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


@unittest.skipUnless(shutil.which('cp'), 'cp is required for copier tests')
class CopyRequestsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source_tree(self):
        source = self.root / 'source'
        (source / 'd0').mkdir(parents=True)
        (source / 'd0' / 'alpha.bin').write_bytes(bytes(range(256)))
        (source / 'd1' / 'nested').mkdir(parents=True)
        (source / 'd1' / 'nested' / 'beta.bin').write_bytes(b'beta' * 100)
        (source / 'empty').mkdir()
        (source / 'root.txt').write_bytes(b'root')
        return source

    def test_each_request_copies_the_complete_private_tree(self):
        source = self.source_tree()
        destinations_root = self.root / 'destinations'
        destinations = prepare_destinations(destinations_root, 3)
        self.assertEqual(
            destinations,
            [destinations_root / f'request_{index}' / 'tree' for index in range(3)],
        )
        self.assertEqual(len(set(destinations)), 3)
        for destination in destinations:
            self.assertFalse(destination.exists())

        wall, results = copy_requests(source, destinations, CP_R)

        self.assertGreaterEqual(wall, 0.0)
        self.assertEqual(len(results), 3)
        expected = tree_inventory(source)
        self.assertIsNone(expected['empty'])
        for destination in destinations:
            self.assertEqual(tree_inventory(destination), expected)
            verify_tree(source, destination, expected, contents=True)
            for relative, size in expected.items():
                if size is None:
                    self.assertTrue((destination / relative).is_dir())
                else:
                    self.assertEqual(
                        (destination / relative).read_bytes(),
                        (source / relative).read_bytes(),
                    )
        for result in results:
            self.assertAlmostEqual(result.latency_s, result.copy_s + result.fsync_s)
            self.assertGreaterEqual(wall, result.latency_s)

        # Regular copies stay write-independent in both directions.
        first, second = destinations[0], destinations[1]
        (first / 'root.txt').write_bytes(b'destination mutation')
        self.assertEqual((source / 'root.txt').read_bytes(), b'root')
        self.assertEqual((second / 'root.txt').read_bytes(), b'root')
        (source / 'root.txt').write_bytes(b'source mutation')
        self.assertEqual((first / 'root.txt').read_bytes(), b'destination mutation')
        self.assertEqual((second / 'root.txt').read_bytes(), b'root')
        self.assertTrue((first / 'empty').is_dir())


class WorkloadPlanTests(unittest.TestCase):
    def test_concurrency_sweep_keeps_shape_fixed_per_kind(self):
        args = argparse.Namespace(
            stage='concurrency', byte_files=3, concurrency_large_bytes=3072,
            concurrency_small_files=7, small_file_size=64, conc_levels=(1, 3, 5),
        )
        plan = build_plan(args)
        for kind, shape in (('large', (3, 1024)), ('small', (7, 64))):
            cases = [spec for spec in plan if spec.kind == kind]
            self.assertEqual({spec.concurrency for spec in cases}, {1, 3, 5})
            self.assertEqual({(spec.files, spec.file_size) for spec in cases}, {shape})


class DatasetShapeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_dataset_is_not_sharded_by_request_concurrency(self):
        inventories = []
        for concurrency in (1, 4):
            spec = Workload('files', 'small', 7, 64, concurrency)
            source = self.root / f'source-{concurrency}'
            gen_dataset(spec, source, files_per_dir=3)
            inventories.append(tree_inventory(source))

        self.assertEqual(inventories[0], inventories[1])
        files = {
            key: size for key, size in inventories[0].items() if size is not None
        }
        self.assertEqual(sorted(files.values()), [64] * 7)
        self.assertEqual(
            {Path(key).parent.name for key in files},
            {'d_0', 'd_1', 'd_2'},
        )
        self.assertEqual(
            sum(1 for size in inventories[0].values() if size is None),
            5,  # '.', three populated data directories, one empty directory
        )


if __name__ == '__main__':
    unittest.main()
