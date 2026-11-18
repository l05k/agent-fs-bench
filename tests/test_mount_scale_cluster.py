"""Lifecycle worker invariants; placement lives in tests/test_cluster.py."""

import sys
import tempfile
from pathlib import Path
import unittest

from agent_fs_bench import cluster
from agent_fs_bench.benchmarks.mount_scale_cluster import Lifecycle, Worker


class WorkerDataTests(unittest.TestCase):
    def test_worker_requires_same_shared_namespace_before_creating_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            admin, work = root / 'admin', root / 'staging'
            admin.mkdir()
            work.mkdir()
            data = admin / 'data'
            data.mkdir()
            (data / '.owner').write_text('another-volume')
            profile = f'''[profiles.fs]
topology = "multi-mount"
admin_root = "{admin}"
private_mount = ["mount", "{{subdir}}", "{{mountpoint}}"]
unmount = ["umount", "{{mountpoint}}"]
'''
            lifecycle = Lifecycle({'profile': profile, 'target': 'fs',
                                   'data_root': str(data), 'work_dir': str(work),
                                   'name': 'local', 'token': 'expected',
                                   'command_timeout_s': 5, 'idle_sample_s': 0.1})
            with self.assertRaisesRegex(AssertionError, 'run sentinel'):
                lifecycle.setup({'repeat': 1, 'clients': 2, 'total_clients': 2})
            self.assertFalse((data / 'N2-R1').exists())


class WorkerDeadlineTests(unittest.TestCase):
    def test_mount_lifecycle_replies_can_exceed_the_command_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker_program = root / 'worker'
            worker_program.write_text(f'#!{sys.executable}\n' + '''import json, sys, time
json.loads(sys.stdin.readline())
print('{}', flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['action'] != 'finish':
        time.sleep(0.1)
    print(json.dumps({'completed': request['action']}), flush=True)
    if request['action'] == 'finish':
        break
''')
            worker_program.chmod(0o755)
            node = cluster.Node('local', None, root, str(worker_program), {})
            worker = Worker(node, {'lifecycle_timeout_s': 5}, root / 'logs', 5, 5)
            self.addCleanup(worker.process.stdout.close)
            try:
                worker.timeout_s = 0.01
                for action in ('mount', 'unmount', 'mount-idle', 'unmount-idle',
                               'mount-plan', 'verify-mount'):
                    with self.subTest(action=action):
                        self.assertEqual(worker.call(action), {'completed': action})
            finally:
                worker.timeout_s = 5
                worker.close()


if __name__ == '__main__':
    unittest.main()
