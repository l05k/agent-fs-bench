"""Lifecycle worker invariants; placement lives in tests/test_cluster.py."""

import tempfile
from pathlib import Path
import unittest

from agent_fs_bench.benchmarks.mount_scale_cluster import Lifecycle


class WorkerDataTests(unittest.TestCase):
    def test_worker_requires_same_shared_namespace_before_creating_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, work = root / 'data', root / 'staging'
            data.mkdir()
            work.mkdir()
            (data / '.owner').write_text('another-volume')
            lifecycle = Lifecycle({'directory_smoke': True, 'data_root': str(data),
                                   'work_dir': str(work), 'name': 'local', 'token': 'expected'})
            with self.assertRaisesRegex(AssertionError, 'run sentinel'):
                lifecycle.setup({'repeat': 1, 'clients': 2, 'total_clients': 2})
            self.assertFalse((data / 'N2-R1').exists())


if __name__ == '__main__':
    unittest.main()
