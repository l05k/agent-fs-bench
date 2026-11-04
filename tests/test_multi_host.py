import contextlib
from dataclasses import asdict
import io
from pathlib import Path
import platform
import tempfile
import unittest

from agent_fs_bench.benchmarks import multi_host


class WorkerMetadataTests(unittest.TestCase):
    def test_real_worker_reports_its_runtime_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = root / 'hosts'
            hosts.mkdir()
            shared = root / 'shared'
            shared.mkdir()
            workload = multi_host.WorkloadConfig(
                files_per_host=2, file_size_bytes=32, directories=1,
                shared_files_per_host=2, rounds=1,
                fsync=False, read_back=True, cleanup_private=True,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                result = multi_host._run_worker_job(
                    {'host_name': 'worker', 'workload': asdict(workload)},
                    (root, hosts / 'worker', shared),
                )
            self.assertEqual(result['machine'], platform.machine())
            self.assertEqual(result['platform'], platform.platform())
            self.assertTrue(result['success'])


if __name__ == '__main__':
    unittest.main()
