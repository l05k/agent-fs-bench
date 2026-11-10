import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import npm_install as bench
from agent_fs_bench.benchmarks import npm_install_nofsync


class DurabilityTests(unittest.TestCase):
    def test_project_flush_persists_parent_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            package = project / 'node_modules' / 'pkg'
            package.mkdir(parents=True)
            payload = package / 'index.js'
            payload.write_text('module.exports = 1;\n')
            (project / 'package-lock.json').write_text('{}')
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'payload').write_text('not part of the project')
            (package / 'alias').symlink_to('index.js')
            (package / 'missing').symlink_to('does-not-exist')
            (package / 'outside-file').symlink_to(outside / 'payload')
            (package / 'outside-dir').symlink_to(outside, target_is_directory=True)
            synced = []
            actual_fsync = os.fsync

            def record_sync(fd):
                item = os.fstat(fd)
                synced.append((item.st_dev, item.st_ino))
                actual_fsync(fd)

            def identity(path):
                item = path.stat()
                return item.st_dev, item.st_ino

            with mock.patch.object(bench.os, 'fsync', side_effect=record_sync):
                bench.fsync_tree(project)
            self.assertEqual(synced.count(identity(payload)), 1)
            self.assertNotIn(identity(outside), synced)
            self.assertNotIn(identity(outside / 'payload'), synced)
            self.assertIn(identity(project / 'package-lock.json'), synced)
            self.assertLess(synced.index(identity(package)),
                            synced.index(identity(project / 'node_modules')))
            self.assertEqual(synced[-1], identity(project))

    def test_flush_time_includes_open_and_close_overhead(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'payload').write_bytes(b'data')
            clock = [0.0]
            actual_open, actual_close = os.open, os.close

            def delayed_open(*args, **kwargs):
                clock[0] += 0.25
                return actual_open(*args, **kwargs)

            def delayed_close(fd):
                clock[0] += 0.25
                return actual_close(fd)

            with mock.patch.object(bench.time, 'monotonic', side_effect=lambda: clock[0]), \
                    mock.patch.object(bench.os, 'open', side_effect=delayed_open), \
                    mock.patch.object(bench.os, 'close', side_effect=delayed_close):
                elapsed = bench.fsync_tree(project)
            # One regular file and its containing directory: both open/close pairs
            # belong to the measured completion latency, not just fsync syscalls.
            self.assertEqual(elapsed, 1.0)


class ConcurrencyEnvTests(unittest.TestCase):
    def test_each_manager_pins_its_own_pool(self):
        self.assertEqual(bench.concurrency_env('npm', 4), {'UV_THREADPOOL_SIZE': '4'})
        self.assertEqual(bench.concurrency_env('pnpm', 4), {'RAYON_NUM_THREADS': '4'})

    def test_zero_keeps_tool_defaults(self):
        self.assertEqual(bench.concurrency_env('npm', 0), {})
        self.assertEqual(bench.concurrency_env('pnpm', 0), {})

    def test_manager_env_replaces_an_inherited_pool(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {'RAYON_NUM_THREADS': '99', 'UV_THREADPOOL_SIZE': '99'}
        ):
            env = bench.manager_env('pnpm', 4, Path(directory))
        self.assertEqual(env['RAYON_NUM_THREADS'], '4')
        self.assertNotIn('UV_THREADPOOL_SIZE', env)

    def test_manager_env_keeps_home_and_tmp_inside_the_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory) / 'sandbox'
            env = bench.manager_env('npm', 4, sandbox)
            home = str((sandbox / 'home').resolve())
            temporary = str((sandbox / 'tmp').resolve())
        self.assertEqual(env['HOME'], home)
        self.assertEqual(env['TMPDIR'], temporary)
        self.assertEqual(env['NPM_CONFIG_USERCONFIG'], str(sandbox / 'user.npmrc'))


class CommandTests(unittest.TestCase):
    def test_npm_replays_the_committed_lockfile_offline(self):
        self.assertEqual(
            bench.install_command('npm', '/usr/bin/npm', Path('/prep/source'), True),
            ['/usr/bin/npm', 'ci', '--offline', '--cache', '/prep/source/cache',
             '--legacy-peer-deps', '--no-audit', '--no-fund', '--no-update-notifier',
             '--ignore-scripts'])

    def test_pnpm_reads_only_the_copied_store_and_cache(self):
        self.assertEqual(
            bench.install_command('pnpm', '/usr/bin/pnpm', Path('/prep/source'), True),
            ['/usr/bin/pnpm', 'install', '--offline', '--frozen-lockfile',
             '--store-dir', '/prep/source/store', '--cache-dir', '/prep/source/cache',
             '--package-import-method=copy',
             '--config.enable-global-virtual-store=false', '--reporter=append-only',
             '--ignore-scripts'])

    def test_lifecycle_scripts_run_only_when_the_experiment_asks(self):
        self.assertNotIn(
            '--ignore-scripts',
            bench.install_command('npm', 'npm', Path('/prep/source'), False))
        self.assertNotIn(
            '--ignore-scripts',
            bench.install_command('pnpm', 'pnpm', Path('/prep/source'), False))


class BatchMetricTests(unittest.TestCase):
    TREES = [
        {'files': 3, 'dirs': 1, 'symlinks': 0, 'hardlinks': 0,
         'logical_bytes': 10, 'allocated_bytes': 10, 'du_kib': 1},
        {'files': 5, 'dirs': 2, 'symlinks': 1, 'hardlinks': 0,
         'logical_bytes': 20, 'allocated_bytes': 20, 'du_kib': 2},
    ]

    def test_durable_batch_takes_the_barrier_wall_and_the_slowest_install(self):
        samples = [
            {'host': 'local', 'install_s': 2.0, 'flush_s': 1.0, 'durable_s': 3.0,
             'user_s': 0.4, 'sys_s': 0.2},
            {'host': 'node-b', 'install_s': 1.6, 'flush_s': 0.2, 'durable_s': 1.8,
             'user_s': 0.3, 'sys_s': 0.1},
        ]
        result = bench.batch_metrics(samples, self.TREES, wall=3.4)
        self.assertEqual(result['batch_install_s'], 2.0)
        self.assertEqual(result['batch_durable_s'], 3.4)
        self.assertEqual(result['install_mean_s'], 1.8)
        self.assertEqual(result['flush_mean_s'], 0.6)
        self.assertEqual(result['durable_mean_s'], 2.4)
        self.assertAlmostEqual(result['installs_per_s'], 1.0, places=6)
        self.assertAlmostEqual(result['durable_installs_per_s'], 2 / 3.4, places=6)
        self.assertEqual(result['user_s'], 0.7)
        self.assertEqual(result['sys_s'], 0.3)
        self.assertEqual(result['files'], 8)
        self.assertEqual(result['dirs'], 3)
        self.assertEqual(result['du_kib'], 3)

    def test_install_only_batch_has_no_flush_or_durable_columns(self):
        samples = [
            {'host': 'local', 'install_s': 2.0, 'user_s': 0.4, 'sys_s': 0.2},
            {'host': 'node-b', 'install_s': 1.0, 'user_s': 0.3, 'sys_s': 0.1},
        ]
        result = bench.batch_metrics(samples, self.TREES, wall=2.2, flush=False)
        self.assertEqual(result['batch_install_s'], 2.0)
        self.assertEqual(result['install_mean_s'], 1.5)
        self.assertAlmostEqual(result['installs_per_s'], 1.0, places=6)
        self.assertEqual(result['user_s'], 0.7)
        self.assertEqual(result['files'], 8)
        for key in ('flush_mean_s', 'durable_mean_s',
                    'batch_durable_s', 'durable_installs_per_s'):
            self.assertNotIn(key, result)

    def test_one_task_batch_is_its_own_install(self):
        samples = [{'host': 'local', 'install_s': 2.5, 'flush_s': 0.5,
                    'durable_s': 3.0, 'user_s': 1.0, 'sys_s': 0.5}]
        result = bench.batch_metrics(samples, self.TREES[:1], wall=3.1)
        self.assertEqual(result['batch_install_s'], 2.5)
        self.assertEqual(result['batch_durable_s'], 3.1)
        self.assertAlmostEqual(result['durable_installs_per_s'], 1 / 3.1, places=6)

    def test_install_rows_drop_the_removed_start_offset(self):
        sample = {'host': 'node-b', 'install_s': 2.0, 'flush_s': 0.5, 'durable_s': 2.5,
                  'user_s': 0.4, 'sys_s': 0.2}
        self.assertEqual(list(bench.install_metrics(sample, True)),
                         ['install_s', 'flush_s', 'durable_s', 'user_s', 'sys_s'])
        self.assertEqual(list(bench.install_metrics(sample, False)),
                         ['install_s', 'user_s', 'sys_s'])


class TreeMetricTests(unittest.TestCase):
    def test_inventory_counts_links_without_following_them(self):
        with tempfile.TemporaryDirectory() as directory:
            node_modules = Path(directory) / 'node_modules'
            package = node_modules / 'pkg'
            package.mkdir(parents=True)
            payload = package / 'index.js'
            payload.write_text('module.exports = 1;\n')
            (package / 'alias').symlink_to('index.js')
            (package / 'hard').hardlink_to(payload)
            size = payload.stat().st_size
            metrics = bench.tree_metrics(node_modules)
        self.assertEqual(metrics['dirs'], 2)
        self.assertEqual(metrics['files'], 2)
        self.assertEqual(metrics['symlinks'], 1)
        self.assertEqual(metrics['hardlinks'], 2)
        self.assertEqual(metrics['logical_bytes'], 2 * size)
        self.assertGreater(metrics['du_kib'], 0)


class PlacementTests(unittest.TestCase):
    def test_same_device_and_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, prep = root / 'target', root / 'prep'
            target.mkdir()
            prep.mkdir()

            def fake_fstype(path):
                path.resolve()
                return 'ext4'

            with mock.patch.object(bench, 'fstype', side_effect=fake_fstype):
                with self.assertRaisesRegex(bench.BenchError, 'different device'):
                    bench.check_placement(target, prep)

    def test_non_local_preparation_filesystem_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, prep = root / 'target', root / 'prep'
            target.mkdir()
            prep.mkdir()
            prep_resolved = prep.resolve()

            def fake_fstype(path):
                return 'nfs' if path.resolve() == prep_resolved else 'ext4'

            with mock.patch.object(bench, 'fstype', side_effect=fake_fstype):
                with self.assertRaisesRegex(bench.BenchError, 'local filesystem'):
                    bench.check_placement(target, prep)

    def test_separate_device_and_type_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, prep = root / 'target', root / 'prep'
            target.mkdir()
            prep.mkdir()
            prep_resolved = prep.resolve()

            def fake_fstype(path):
                return 'tmpfs' if path.resolve() == prep_resolved else 'ext4'

            def fake_stat(self, *args, **kwargs):
                # os.path.realpath, not Path.resolve: resolve() calls stat() itself.
                return types.SimpleNamespace(
                    st_dev=2 if os.path.realpath(self) == str(prep_resolved) else 1)

            with mock.patch.object(bench, 'fstype', side_effect=fake_fstype), \
                    mock.patch.object(Path, 'stat', fake_stat):
                self.assertEqual(bench.check_placement(target, prep), ('ext4', 'tmpfs'))

    def test_project_outside_the_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            (target / 'project').mkdir(parents=True)
            with self.assertRaisesRegex(bench.BenchError, 'outside the measured target'):
                bench.check_project_placement(root / 'project', target)
            bench.check_project_placement(target / 'project', target)


class ConfigTests(unittest.TestCase):
    def test_a_sweep_over_managers_is_rejected(self):
        with self.assertRaisesRegex(bench.BenchError, 'pm must be one of'):
            bench.parse_args([], {**bench.CONFIG, 'pm': 'both'})

    def test_repeats_must_be_positive(self):
        with self.assertRaisesRegex(bench.BenchError, 'repeats must be a positive integer'):
            bench.parse_args([], {**bench.CONFIG, 'repeats': 0})

    def test_nofsync_workload_exposes_flush_off(self):
        self.assertFalse(npm_install_nofsync.parse_args([]).flush)
        self.assertTrue(bench.parse_args([]).flush)


if __name__ == '__main__':
    unittest.main()
