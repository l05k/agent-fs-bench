import contextlib
from dataclasses import asdict
import io
from pathlib import Path
import statistics
import tempfile
import unittest

from agent_fs_bench.benchmarks import multi_host
from agent_fs_bench.results import RunContext, Tables, Target, read_table


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

            tables_dir = root / 'tables'
            tables_dir.mkdir()
            tables = Tables(tables_dir)
            ctx = RunContext('multi-host', 'run', Target('fs', root),
                             root / 'work', root / 'res', tables)
            multi_host._emit_measurements(ctx, 'worker', result['metrics'])
            tables.close()

            counts = {
                'private_create': 3, 'private_fsync': 5, 'private_read': 3,
                'private_stat': 3, 'private_rename': 3,
                'shared_create': 2, 'shared_fsync': 3, 'shared_read': 2,
                'shared_stat': 2, 'shared_delete': 2,
            }
            phases = read_table(tables_dir / 'phases.csv')
            samples = read_table(tables_dir / 'latencies.csv')
            self.assertEqual(len(phases), 2 * len(counts))
            self.assertEqual(len(samples), 2 * sum(counts.values()))

            phase_rows = {(int(row['repeat']), row['phase']): row for row in phases}
            self.assertEqual(set(phase_rows),
                             {(repeat, phase) for repeat in (1, 2) for phase in counts})
            sample_rows: dict[tuple[int, str], list[int]] = {}
            for row in samples:
                self.assertEqual(row['host'], 'worker')
                sample_rows.setdefault((int(row['repeat']), row['phase']), []).append(
                    int(row['latency_ns']))
            for group, latencies in sample_rows.items():
                self.assertEqual(len(latencies), counts[group[1]])
                row = phase_rows[group]
                self.assertEqual(row['host'], 'worker')
                self.assertEqual(int(row['ops']), counts[group[1]])
                self.assertAlmostEqual(float(row['mean_ms']),
                                       statistics.fmean(latencies) / 1_000_000)
                self.assertAlmostEqual(float(row['ops_per_s']),
                                       counts[group[1]] / float(row['elapsed_s']))
                expected_bytes = 32 * counts[group[1]] if group[1].endswith(('create', 'read')) else 0
                self.assertEqual(int(row['bytes']), expected_bytes)

            renamed = sorted((hosts / 'worker').rglob('*.renamed'))
            self.assertEqual(len(renamed), 6)
            self.assertTrue(all(path.read_bytes() == multi_host._payload('worker', 32)
                                for path in renamed))
            self.assertEqual(list(shared.iterdir()), [foreign])
            self.assertEqual(foreign.read_bytes(), b'untouched')


if __name__ == '__main__':
    unittest.main()
