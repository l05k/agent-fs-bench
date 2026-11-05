import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import npm_install as bench


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
                    'env': os.environ.copy(),
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
                'env': os.environ.copy(),
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


if __name__ == '__main__':
    unittest.main()
