import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import npm_install as bench
from agent_fs_bench.results import RunContext, Tables, read_table
from agent_fs_bench.targets import Target

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

class InstallBatchTests(unittest.TestCase):
    def test_independent_install_workers_overlap_and_report_durable_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = []
            for index in range(2):
                project = root / f'project-{index}'
                project.mkdir()
                program = (
                    'from pathlib import Path; import sys, time\n'
                    'root, project, index = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]\n'
                    '(root / ("ready-" + index)).touch()\n'
                    'deadline = time.monotonic() + 10\n'
                    'while not all((root / ("ready-" + str(i))).exists() for i in range(2)):\n'
                    '    if time.monotonic() >= deadline: raise RuntimeError("workers ran serially")\n'
                    '    time.sleep(0.01)\n'
                    '(project / "node_modules").mkdir()\n'
                    '(project / "node_modules" / "payload").write_text(index)\n'
                )
                jobs.append({
                    'project': project, 'log': root / f'worker-{index}.log',
                    'command': [sys.executable, '-c', program, str(root), str(project), str(index)],
                    'env': os.environ.copy(), 'flush': True,
                })
            tasks = bench.run_workers(jobs)
            for index, task in enumerate(tasks):
                payload = jobs[index]['project'] / 'node_modules' / 'payload'
                self.assertEqual(payload.read_text(), str(index))
                self.assertAlmostEqual(task['durable_s'], task['install_s'] + task['flush_s'])
                self.assertGreaterEqual(task['start_offset_s'], 0)
                self.assertEqual(task['task'], index + 1)

    def test_failed_install_aborts_batch_instead_of_reporting_throughput(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = [{
                'project': root, 'log': root / 'failure.log',
                'command': [sys.executable, '-c', 'import sys; sys.exit(23)'],
                'env': os.environ.copy(), 'flush': True,
            }]
            with self.assertRaisesRegex(bench.BenchError, 'install exited 23'):
                bench.run_workers(jobs)

    def test_throughput_uses_batch_span_including_start_offsets(self):
        tasks = [
            {'start_offset_s': 0.0, 'install_s': 2.0, 'flush_s': 1.0,
             'durable_s': 3.0, 'user_s': 0.4, 'sys_s': 0.2},
            {'start_offset_s': 0.7, 'install_s': 1.6, 'flush_s': 0.2,
             'durable_s': 1.8, 'user_s': 0.3, 'sys_s': 0.1},
        ]
        result = bench.batch_metrics(tasks, [{'files': 3}, {'files': 5}])
        self.assertEqual(result['batch_install_s'], 2.3)
        self.assertEqual(result['batch_durable_s'], 3.0)
        self.assertEqual(result['install_mean_s'], 1.8)
        self.assertEqual(result['durable_mean_s'], 2.4)
        self.assertAlmostEqual(result['installs_per_s'], 2 / 2.3, places=6)
        self.assertAlmostEqual(result['durable_installs_per_s'], 2 / 3.0, places=6)
        self.assertEqual(result['files'], 8)

class NoFlushTests(unittest.TestCase):
    def make_batch(self, root, flush):
        target = root / 'target'
        work = target / 'agent-fs-bench' / 'npm-install-nofsync' / 'run'
        work.mkdir(parents=True)
        result = root / 'results'
        (result / 'logs').mkdir(parents=True)
        source = root / 'source' / 'cache'
        source.mkdir(parents=True)
        (source / 'payload').write_bytes(b'cache')
        template = root / 'template'
        template.mkdir()
        (template / 'package.json').write_text('{"name":"fixture"}')
        (template / 'package-lock.json').write_text('{}')
        prep = root / 'prep'
        prep.mkdir()
        fake = root / 'fake-npm'
        fake.write_text('#!/bin/sh\nmkdir -p node_modules/pkg\n'
                        'printf "module.exports = 1;\\n" > node_modules/pkg/index.js\n')
        fake.chmod(0o755)
        ctx = RunContext('npm-install-nofsync', 'run', Target('t', target),
                         work, result, Tables(result))
        args = bench.parse_args([], {
            **bench.CONFIG, 'flush': flush, 'concurrency': 0,
        })
        return ctx, args, str(fake), source, template, prep

    def test_run_batch_without_flush_never_syncs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx, args, fake, source, template, prep = self.make_batch(root, False)
            with mock.patch.object(bench, 'sync_filesystem') as sync_filesystem, \
                    mock.patch.object(bench, 'fsync_tree') as fsync_tree, \
                    mock.patch.object(bench, 'fsync_path') as fsync_path:
                metrics, tasks = bench.run_batch(
                    ctx, args, 'npm', fake, source, template, prep, 1, 1)
            sync_filesystem.assert_not_called()
            fsync_tree.assert_not_called()
            fsync_path.assert_not_called()
            self.assertIn('install_mean_s', metrics)
            self.assertNotIn('durable_mean_s', metrics)
            self.assertNotIn('flush_s', tasks[0])

    def test_run_batch_with_flush_syncs_before_and_after(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx, args, fake, source, template, prep = self.make_batch(root, True)
            with mock.patch.object(bench, 'sync_filesystem') as sync_filesystem, \
                    mock.patch.object(bench, 'fsync_tree') as fsync_tree, \
                    mock.patch.object(bench, 'fsync_path') as fsync_path:
                metrics, tasks = bench.run_batch(
                    ctx, args, 'npm', fake, source, template, prep, 1, 1)
            self.assertEqual(sync_filesystem.call_count, 2)
            self.assertEqual(fsync_tree.call_count, 1)
            self.assertTrue(fsync_path.called)
            self.assertIn('durable_mean_s', metrics)
            self.assertIn('flush_s', tasks[0])

    def test_placement_is_enforced_without_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx, args, fake, source, template, prep = self.make_batch(root, False)
            ctx.work_dir = root / 'outside-target'
            with self.assertRaisesRegex(bench.BenchError, 'outside the measured target'):
                bench.run_batch(ctx, args, 'npm', fake, source, template, prep, 1, 1)

    def test_seeding_sync_is_skipped_without_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.mkdir()
            prep_root = root / 'prep'
            prep_root.mkdir()
            result = root / 'results'
            result.mkdir()
            ctx = RunContext('npm-install-nofsync', 'run', Target('t', target),
                             target / 'work', result, Tables(result))

            def seed(binary, cache, seed_dir, template, fixture, ignore, concurrency):
                (template / 'package.json').write_text('{"name":"fixture"}')
                (template / 'package-lock.json').write_text('{}')

            actual_is_file = Path.is_file

            def mountinfo_only(path):
                return True if str(path) == '/proc/self/mountinfo' else actual_is_file(path)

            for flush in (False, True):
                args = bench.parse_args([], {
                    **bench.CONFIG, 'pm': 'npm', 'runs': 1, 'flush': flush,
                    'prep_dir': str(prep_root), 'seed_only': True,
                })
                with mock.patch.object(Path, 'is_file', mountinfo_only), \
                        mock.patch.object(bench, 'check_placement',
                                          return_value=('ext4', 'tmpfs')), \
                        mock.patch.object(bench, 'checked_tools',
                                          return_value=({'npm': '/fake/npm'},
                                                        {'npm': '1', 'node': '1'})), \
                        mock.patch.object(bench, 'fixture_dir_for', return_value=root), \
                        mock.patch.object(bench, 'seed_npm_fixture', side_effect=seed), \
                        mock.patch.object(bench.os, 'sched_getaffinity',
                                          return_value={0, 1, 2, 3}, create=True), \
                        mock.patch.object(bench, 'sync_filesystem') as sync_filesystem:
                    bench.run(ctx, args)
                if flush:
                    self.assertEqual(sync_filesystem.call_count, 1)
                else:
                    sync_filesystem.assert_not_called()
                self.assertEqual(ctx.info['target_fstype'], 'ext4')
                self.assertEqual(ctx.info['prep_fstype'], 'tmpfs')
                shutil.rmtree(prep_root / 'agent-fs-bench')

    def test_batch_metrics_omit_flush_and_durable_phases(self):
        tasks = [
            {'start_offset_s': 0.0, 'install_s': 2.0, 'user_s': 0.4, 'sys_s': 0.2},
            {'start_offset_s': 0.5, 'install_s': 1.0, 'user_s': 0.3, 'sys_s': 0.1},
        ]
        result = bench.batch_metrics(tasks, [{'files': 1}], flush=False)
        self.assertEqual(result['batch_install_s'], 2.0)
        self.assertEqual(result['install_mean_s'], 1.5)
        self.assertEqual(result['user_s'], 0.7)
        self.assertEqual(result['sys_s'], 0.3)
        self.assertAlmostEqual(result['installs_per_s'], 1.0, places=6)
        for key in ('flush_mean_s', 'durable_mean_s',
                    'batch_durable_s', 'durable_installs_per_s'):
            self.assertNotIn(key, result)

    def test_noflush_worker_skips_fsync_and_durable_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'payload').write_bytes(b'data')
            connection = mock.MagicMock()
            with mock.patch.object(bench, 'fsync_tree') as fsync_tree:
                bench.install_worker(
                    [sys.executable, '-c', 'pass'], project,
                    project / 'worker.log', os.environ.copy(),
                    mock.MagicMock(), connection, False,
                )
            fsync_tree.assert_not_called()
            sample = connection.send.call_args.args[0]
            self.assertIn('install_s', sample)
            self.assertNotIn('flush_s', sample)
            self.assertNotIn('durable_s', sample)

    def test_nofsync_workload_exposes_flush_off(self):
        from agent_fs_bench.benchmarks import npm_install_nofsync
        self.assertFalse(npm_install_nofsync.CONFIG['flush'])
        self.assertFalse(npm_install_nofsync.parse_args([]).flush)
        self.assertTrue(bench.parse_args([]).flush)

class TableTests(unittest.TestCase):
    def run_workload(self, root, ctx, args, metrics, tasks):
        def seed(binary, cache, seed_dir, template, fixture, ignore, concurrency):
            (template / 'package.json').write_text('{"name":"fixture"}')
            (template / 'package-lock.json').write_text('{}')

        actual_is_file = Path.is_file

        def mountinfo_only(path):
            return True if str(path) == '/proc/self/mountinfo' else actual_is_file(path)

        with mock.patch.object(Path, 'is_file', mountinfo_only), \
                mock.patch.object(bench, 'check_placement', return_value=('ext4', 'tmpfs')), \
                mock.patch.object(bench, 'checked_tools',
                                  return_value=({'npm': '/fake/npm'},
                                                {'npm': '1', 'node': '1'})), \
                mock.patch.object(bench, 'fixture_dir_for', return_value=root), \
                mock.patch.object(bench, 'seed_npm_fixture', side_effect=seed), \
                mock.patch.object(bench.os, 'sched_getaffinity',
                                  return_value={0, 1, 2, 3}, create=True), \
                mock.patch.object(bench, 'run_batch', return_value=(metrics, tasks)):
            bench.run(ctx, args)

    def make_context(self, root, prep_root):
        target = root / 'target'
        target.mkdir()
        prep_root.mkdir()
        result = root / 'results'
        result.mkdir()
        ctx = RunContext('npm-install', 'run', Target('fs', target),
                         target / 'work', result, Tables(result))
        return ctx, result

    def test_run_writes_batch_and_task_rows_and_discovered_info(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx, result = self.make_context(root, root / 'prep')
            args = bench.parse_args([], {
                **bench.CONFIG, 'pm': 'npm', 'runs': 2, 'concurrent_jobs': [1, 2],
                'prep_dir': str(root / 'prep'),
            })
            metrics = {
                'install_mean_s': 1.0, 'flush_mean_s': 0.5, 'durable_mean_s': 1.5,
                'batch_install_s': 1.0, 'batch_durable_s': 1.5, 'installs_per_s': 1.0,
                'durable_installs_per_s': 0.5, 'user_s': 0.4, 'sys_s': 0.2,
                'files': 3, 'dirs': 1, 'symlinks': 0, 'hardlinks': 0,
                'logical_bytes': 10, 'allocated_bytes': 10, 'du_kib': 1,
            }
            tasks = [{'task': 1, 'install_s': 1.0, 'flush_s': 0.5, 'durable_s': 1.5,
                      'user_s': 0.4, 'sys_s': 0.2, 'start_offset_s': 0.0}]
            self.run_workload(root, ctx, args, metrics, tasks)
            ctx.tables.close()

            batches = read_table(result / 'batches.csv')
            self.assertEqual(
                {(row['manager'], row['concurrent_jobs'], row['repeat']) for row in batches},
                {('npm', '1', '1'), ('npm', '1', '2'), ('npm', '2', '1'), ('npm', '2', '2')})
            self.assertEqual(float(batches[0]['install_mean_s']), 1.0)
            self.assertEqual(float(batches[0]['du_kib']), 1.0)
            samples = read_table(result / 'tasks.csv')
            self.assertEqual(len(samples), 4)
            self.assertEqual({row['sample'] for row in samples}, {'1'})
            self.assertEqual(float(samples[0]['durable_s']), 1.5)
            self.assertEqual(float(samples[0]['start_offset_s']), 0.0)
            self.assertEqual(ctx.info['target_fstype'], 'ext4')
            self.assertEqual(ctx.info['prep_fstype'], 'tmpfs')
            self.assertEqual(ctx.info['tool_versions'], {'npm': {'node': '1', 'npm': '1'}})
            self.assertEqual(ctx.info['seed_input_sha256'], {
                'npm': {'package.json': hashlib.sha256(b'{"name":"fixture"}').hexdigest(),
                        'package-lock.json': hashlib.sha256(b'{}').hexdigest()},
            })
            self.assertIn('cpu_count', ctx.info)
            self.assertFalse((root / 'prep' / 'agent-fs-bench' / 'npm-install'
                              / 'run' / 'fs').exists())

    def test_without_flush_task_rows_omit_durable_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx, result = self.make_context(root, root / 'prep')
            args = bench.parse_args([], {
                **bench.CONFIG, 'pm': 'npm', 'runs': 1, 'flush': False,
                'prep_dir': str(root / 'prep'),
            })
            metrics = {
                'install_mean_s': 1.0, 'batch_install_s': 1.0, 'installs_per_s': 1.0,
                'user_s': 0.4, 'sys_s': 0.2, 'files': 3, 'dirs': 1, 'symlinks': 0,
                'hardlinks': 0, 'logical_bytes': 10, 'allocated_bytes': 10, 'du_kib': 1,
            }
            tasks = [{'task': 1, 'install_s': 1.0, 'user_s': 0.4, 'sys_s': 0.2,
                      'start_offset_s': 0.0}]
            self.run_workload(root, ctx, args, metrics, tasks)
            ctx.tables.close()

            batch_row = read_table(result / 'batches.csv')[0]
            task_row = read_table(result / 'tasks.csv')[0]
            for column in ('flush_mean_s', 'durable_mean_s', 'batch_durable_s',
                           'durable_installs_per_s'):
                self.assertNotIn(column, batch_row)
            for column in ('flush_s', 'durable_s'):
                self.assertNotIn(column, task_row)
            self.assertEqual(float(task_row['install_s']), 1.0)

class PlacementTests(unittest.TestCase):
    def test_placement_accepts_path_objects_from_run(self):
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

class ParseArgsTests(unittest.TestCase):
    def test_duplicate_or_nonpositive_batch_sizes_are_rejected(self):
        for jobs in ([2, 2], [0], [1, -1]):
            with self.subTest(jobs=jobs), self.assertRaises(bench.BenchError):
                bench.parse_args([], {**bench.CONFIG, 'concurrent_jobs': jobs})

if __name__ == '__main__':
    unittest.main()
