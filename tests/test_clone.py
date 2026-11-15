import argparse
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_fs_bench.benchmarks.clone as clone
from agent_fs_bench.benchmarks.clone import (
    BenchmarkError,
    Copier,
    PolarFs,
    Workload,
    build_plan,
    gen_dataset,
    prepare_destination,
    tree_inventory,
    validate_copier,
    verify_tree,
)


def gnu_cp():
    executable = shutil.which('cp')
    if executable is None:
        return None
    version = subprocess.run([executable, '--version'], capture_output=True, text=True)
    return executable if version.returncode == 0 and 'GNU coreutils' in version.stdout else None


GNU_CP = gnu_cp()
KEY_ID = 'fixture-key-id'
KEY_MATERIAL = 'fixture-key-material'
TOKEN_VALUE = 'fixture-token'
NATIVE = {
    'instance_id': 'pfs-test',
    'region_id': 'cn-hangzhou',
    'endpoint': 'polardb.cn-hangzhou.aliyuncs.com',
}

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


@unittest.skipUnless(GNU_CP, 'GNU cp with --reflink is required for copier tests')
class CopierValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()

    def test_real_recursive_copy_passes_and_cleans_up(self):
        validate_copier(Copier('cp-r', (GNU_CP, *clone.CP_ARGS)), self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_hardlinked_tree_is_rejected_and_cleans_up(self):
        hardlink = Copier('cp-rl', (GNU_CP, '-rl', '{src}', '{dst}'))
        with self.assertRaises(BenchmarkError):
            validate_copier(hardlink, self.scratch)
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
        build_plan(argparse.Namespace(**{**clone.CONFIG, 'tasks': 0}))

    def test_unknown_copier_names_fail_instead_of_measuring_something_else(self):
        for selection in (
                {'copier': 'lakebase'},
                {'target_copiers': {'fs': 'lakebase'}},
                {'target_copiers': {'fs': ['polarfs']}},
                {'copier': ['cp-r']}):
            args = argparse.Namespace(**{**clone.CONFIG, **selection})
            with self.subTest(selection=selection), \
                    self.assertRaisesRegex(BenchmarkError, 'copier'):
                build_plan(args)

    def test_native_settings_reject_typos_missing_values_and_escaping_roots(self):
        invalid = (
            {'instance_id': 'pfs-test', 'region_id': 'cn-hangzhou'},
            {**NATIVE, 'volume_root': 'relative/root'},
            {**NATIVE, 'volume_root': '/volume/../escape'},
            {**NATIVE, 'secret': 'do-not-store'},
            {**NATIVE, 'instance_id': ''},
        )
        for settings in invalid:
            args = argparse.Namespace(**{
                **clone.CONFIG, 'polarfs': {'fs': settings}})
            with self.subTest(settings=settings), self.assertRaises(BenchmarkError):
                build_plan(args)

    def test_native_settings_normalize_an_explicit_volume_root(self):
        self.assertEqual(clone.polarfs_settings({**NATIVE, 'volume_root': '/agents/agent-x/'}),
                         (NATIVE['instance_id'], NATIVE['region_id'], NATIVE['endpoint'],
                          '/agents/agent-x'))
        self.assertEqual(clone.polarfs_settings(NATIVE)[-1], '/')

    def test_cp_resolution_uses_the_fixed_materialized_baseline(self):
        args = argparse.Namespace(**clone.CONFIG)
        version = SimpleNamespace(returncode=0, stdout='cp (GNU coreutils) 9.4\n')
        with mock.patch.object(clone.shutil, 'which', return_value='/usr/bin/cp'), \
                mock.patch.object(clone.subprocess, 'run', return_value=version):
            copier = clone.resolve_copier(args, 'fs', Path('/mnt/fs'))
        self.assertEqual(copier.argv,
                         ('/usr/bin/cp', '-r', '--reflink=never', '{src}', '{dst}'))
        self.assertIsNone(copier.polarfs)

    def test_native_resolution_requires_settings_and_credentials(self):
        args = argparse.Namespace(**{
            **clone.CONFIG, 'target_copiers': {'fs': 'polarfs'}})
        with self.assertRaisesRegex(BenchmarkError, 'polarfs settings for fs'):
            clone.resolve_copier(args, 'fs', Path('/mnt/fs'))

        args.polarfs = {'fs': NATIVE}
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(BenchmarkError, 'ALIBABA_CLOUD_ACCESS_KEY_ID'):
            clone.resolve_copier(args, 'fs', Path('/mnt/fs'))

    def test_native_resolution_keeps_credentials_out_of_the_copier(self):
        args = argparse.Namespace(**{
            **clone.CONFIG,
            'target_copiers': {'fs': 'polarfs'},
            'polarfs': {'fs': {**NATIVE, 'volume_root': '/agents/agent-x'}},
        })
        environment = {
            'ALIBABA_CLOUD_ACCESS_KEY_ID': KEY_ID,
            'ALIBABA_CLOUD_ACCESS_KEY_SECRET': KEY_MATERIAL,
            'ALIBABA_CLOUD_SECURITY_TOKEN': TOKEN_VALUE,
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            copier = clone.resolve_copier(args, 'fs', Path('/mnt/fs'))
        self.assertEqual(copier.name, 'polarfs')
        self.assertEqual(copier.polarfs.instance_id, 'pfs-test')
        self.assertEqual(copier.polarfs.volume_root, '/agents/agent-x')
        for value in environment.values():
            self.assertNotIn(value, repr(copier))


class PolarFsPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.mount = Path(temporary.name)

    def test_full_volume_mount_maps_the_mount_root_to_slash(self):
        polarfs = PolarFs(**NATIVE, volume_root='/', mount=self.mount)
        self.assertEqual(polarfs.volume_path(self.mount / 'agent-fs-bench' / 'run'),
                         '/agent-fs-bench/run')

    def test_subdirectory_mount_maps_to_its_explicit_volume_root(self):
        polarfs = PolarFs(**NATIVE, volume_root='/agents/agent-x', mount=self.mount)
        self.assertEqual(polarfs.volume_path(self.mount / 'run'), '/agents/agent-x/run')

    def test_paths_outside_the_configured_mount_are_rejected(self):
        polarfs = PolarFs(**NATIVE, volume_root='/', mount=self.mount)
        with self.assertRaisesRegex(BenchmarkError, 'under mounted volume'):
            polarfs.volume_path(self.mount.parent / 'elsewhere')


class NativeCloneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.mount = Path(temporary.name)
        self.source = self.mount / 'run' / 'source'
        self.source.mkdir(parents=True)
        (self.mount / 'run' / 'destination').mkdir()
        self.destination = self.mount / 'run' / 'destination' / 'tree'
        self.polarfs = PolarFs(**NATIVE, volume_root='/agents/agent-x', mount=self.mount)
        self.copier = Copier('polarfs', polarfs=self.polarfs)

    def test_sdk_success_is_the_whole_timed_request_and_paths_are_volume_absolute(self):
        client = mock.Mock()
        client.clone_polar_fs_basic_snapshot.return_value = SimpleNamespace(
            status_code=200, body=SimpleNamespace(request_id='request-id'))
        with mock.patch.object(clone, 'polarfs_client', return_value=client):
            result = clone.copy_request(self.source, self.destination, self.copier)

        request = client.clone_polar_fs_basic_snapshot.call_args.args[0]
        self.assertEqual(request.polar_fs_instance_id, 'pfs-test')
        self.assertEqual(request.source_path, '/agents/agent-x/run/source')
        self.assertEqual(request.target_path, '/agents/agent-x/run/destination/tree')
        self.assertEqual(result.fsync_s, 0.0)
        self.assertEqual(result.copy_s, result.latency_s)

    def test_rejected_response_is_an_error_not_a_timed_row(self):
        client = mock.Mock()
        client.clone_polar_fs_basic_snapshot.return_value = SimpleNamespace(
            status_code=400, body=None)
        with mock.patch.object(clone, 'polarfs_client', return_value=client), \
                self.assertRaisesRegex(BenchmarkError, 'status 400'):
            clone.copy_request(self.source, self.destination, self.copier)

    def test_sdk_exception_is_not_retried_or_returned(self):
        client = mock.Mock()
        client.clone_polar_fs_basic_snapshot.side_effect = RuntimeError('denied')
        with mock.patch.object(clone, 'polarfs_client', return_value=client), \
                self.assertRaisesRegex(BenchmarkError, 'denied'):
            clone.copy_request(self.source, self.destination, self.copier)
        self.assertEqual(client.clone_polar_fs_basic_snapshot.call_count, 1)

    def test_existing_target_fails_before_the_api_call(self):
        self.destination.mkdir()
        with mock.patch.object(clone, 'polarfs_client') as client, \
                self.assertRaisesRegex(BenchmarkError, 'expected absent'):
            clone.copy_request(self.source, self.destination, self.copier)
        client.assert_not_called()

    def test_client_uses_standard_credentials_and_optional_sts_token(self):
        from alibabacloud_polardb20170801 import client as sdk_client

        for token, expected in ((None, None), (TOKEN_VALUE, TOKEN_VALUE)):
            environment = {
                'ALIBABA_CLOUD_ACCESS_KEY_ID': KEY_ID,
                'ALIBABA_CLOUD_ACCESS_KEY_SECRET': KEY_MATERIAL,
            }
            if token is not None:
                environment['ALIBABA_CLOUD_SECURITY_TOKEN'] = token
            with self.subTest(token=token), \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sdk_client, 'Client') as sdk:
                clone.polarfs_client(self.polarfs)
            config = sdk.call_args.args[0]
            self.assertEqual(config.access_key_id, KEY_ID)
            self.assertEqual(config.access_key_secret, KEY_MATERIAL)
            self.assertEqual(config.security_token, expected)
            self.assertEqual(config.region_id, 'cn-hangzhou')
            self.assertEqual(config.endpoint, 'polardb.cn-hangzhou.aliyuncs.com')
            self.assertFalse(config.retry_options.retryable)


class DatasetShapeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_dataset_groups_files_and_includes_an_empty_directory(self):
        source = self.root / 'source'
        gen_dataset(Workload(7, 64), source, files_per_dir=3, seed=1)
        inventory = tree_inventory(source)

        files = {key: size for key, size in inventory.items() if size is not None}
        self.assertEqual(sorted(files.values()), [64] * 7)
        self.assertEqual({Path(key).parent.name for key in files}, {'d_0', 'd_1', 'd_2'})
        self.assertIsNone(inventory['empty'])
        self.assertEqual(
            sum(1 for size in inventory.values() if size is None),
            5,  # '.', three populated data directories, one empty directory
        )

    def test_same_seed_gives_byte_identical_inputs(self):
        trees = []
        for name, seed in (('a', 7), ('b', 7), ('c', 8)):
            gen_dataset(Workload(3, 4096), self.root / name, files_per_dir=2, seed=seed)
            trees.append([(self.root / name / 'd_0' / f'f_{i}.bin').read_bytes()
                          for i in range(2)])
        self.assertEqual(trees[0], trees[1])
        self.assertNotEqual(trees[0], trees[2])


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
