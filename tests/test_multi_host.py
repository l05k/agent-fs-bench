import contextlib
from dataclasses import asdict
import io
from pathlib import Path
import statistics
import tempfile
import unittest

from agent_fs_bench.benchmarks import multi_host


class WorkerMeasurementTests(unittest.TestCase):
    def test_real_worker_preserves_phase_samples_and_owned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = root / 'hosts'
            hosts.mkdir()
            shared = root / 'shared'
            shared.mkdir()
            foreign = shared / 'another-host.bin'
            foreign.write_bytes(b'untouched')
            workload = multi_host.WorkloadConfig(
                files_per_host=3, file_size_bytes=32, directories=1,
                shared_files_per_host=2, rounds=2,
                fsync=True, read_back=True, cleanup_private=False,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                result = multi_host._run_worker_job(
                    {'host_name': 'worker', 'workload': asdict(workload)},
                    (root, hosts / 'worker', shared),
                )
            counts = {
                'private_create': 3, 'private_fsync': 5, 'private_read': 3,
                'private_stat': 3, 'private_rename': 3,
                'shared_create': 2, 'shared_fsync': 3, 'shared_read': 2,
                'shared_stat': 2, 'shared_delete': 2,
            }
            rows = {(row['round'], row['phase']): row for row in result['metrics']}
            self.assertEqual(set(rows), {(r, phase) for r in (1, 2) for phase in counts})
            for (_, phase), row in rows.items():
                samples = row['latencies_ns']
                self.assertEqual(row['ops'], counts[phase])
                self.assertEqual(len(samples), counts[phase])
                self.assertAlmostEqual(row['mean_ms'], statistics.fmean(samples) / 1_000_000)
                self.assertAlmostEqual(row['ops_per_s'], counts[phase] / row['elapsed_s'])
                expected_bytes = 32 * counts[phase] if phase.endswith(('create', 'read')) else 0
                self.assertEqual(row['bytes'], expected_bytes)
            renamed = sorted((hosts / 'worker').rglob('*.renamed'))
            self.assertEqual(len(renamed), 6)
            self.assertTrue(all(path.read_bytes() == multi_host._payload('worker', 32)
                                for path in renamed))
            self.assertEqual(list(shared.iterdir()), [foreign])
            self.assertEqual(foreign.read_bytes(), b'untouched')


if __name__ == '__main__':
    unittest.main()
