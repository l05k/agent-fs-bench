"""Client placement invariants at local and cross-host capacity boundaries."""

import tempfile
from pathlib import Path
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import mount_scale
from agent_fs_bench.benchmarks.mount_scale_cluster import Allocation, Lifecycle, allocate


class PlacementTests(unittest.TestCase):
    def test_local_fills_before_remote_and_keeps_real_cpu_ids(self):
        hosts = [('local', [7, 2, 11]), ('node-a', [6, 0]), ('node-b', [9, 4])]
        for clients, expected in (
            (1, (Allocation('local', (2,)),)),
            (3, (Allocation('local', (2, 7, 11)),)),
            (4, (Allocation('local', (2, 7, 11)), Allocation('node-a', (0,)))),
            (6, (Allocation('local', (2, 7, 11)), Allocation('node-a', (0, 6)),
                 Allocation('node-b', (4,)))),
        ):
            with self.subTest(clients=clients):
                self.assertEqual(allocate(hosts, clients), expected)

    def test_default_fills_inventory_without_oversubscribing(self):
        self.assertEqual(allocate([('local', [8, 3]), ('node-a', [12])], None),
                         (Allocation('local', (3, 8)), Allocation('node-a', (12,))))
        for clients in (0, -1, 4, True, (1, 2)):
            with self.subTest(clients=clients), self.assertRaises(ValueError):
                allocate([('local', [8, 3]), ('node-a', [12])], clients)

    def test_aliases_cannot_duplicate_capacity(self):
        for hosts in ([('local', [0, 0])], [('local', [0]), ('local', [1])],
                      [('local', [])]):
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                allocate(hosts, None)


class SmokeCapacityTests(unittest.TestCase):
    def test_single_affinity_cpu_is_rejected_before_smoke_setup(self):
        args = mount_scale.parse_args(['--directory-smoke'])
        with (mock.patch.object(mount_scale.platform, 'system', return_value='Linux'),
              mock.patch.object(mount_scale.os, 'sched_getaffinity', return_value={3}),
              self.assertRaises(ValueError)):
            mount_scale._validate(args)


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
            with self.assertRaisesRegex(RuntimeError, 'same run sentinel'):
                lifecycle.setup(repeat=1, clients=2, total_clients=2)
            self.assertFalse((data / 'N2-R1').exists())


if __name__ == '__main__':
    unittest.main()
