"""CPU reservation and worker data invariants."""

import tempfile
from pathlib import Path
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import mount_scale
from agent_fs_bench.benchmarks.mount_scale_cluster import Lifecycle, usable_cpus


class ReservationTests(unittest.TestCase):
    def test_highest_cpu_ids_are_reserved_and_real_ids_kept(self):
        self.assertEqual(usable_cpus([7, 2, 11, 5, 0], 2), ([0, 2, 5], [7, 11]))
        self.assertEqual(usable_cpus(list(range(64)), 4), (list(range(60)), [60, 61, 62, 63]))

    def test_reservation_must_leave_a_rank_cpu(self):
        for reserved in (3, 4, -1):
            with self.subTest(reserved=reserved), self.assertRaises(ValueError):
                usable_cpus([0, 1, 2], reserved)


class SmokeCapacityTests(unittest.TestCase):
    def test_single_affinity_cpu_is_rejected_before_smoke_setup(self):
        args = mount_scale.parse_args(['--directory-smoke'])
        with (mock.patch.object(mount_scale.platform, 'system', return_value='Linux'),
              # create=True: os.sched_getaffinity exists only on Linux, while the
              # test exercises the Linux branch from any controller OS.
              mock.patch.object(mount_scale.os, 'sched_getaffinity', return_value={3, 4},
                                create=True),
              self.assertRaisesRegex(ValueError, 'at least 2 usable')):
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
                                   'work_dir': str(work), 'name': 'local', 'token': 'expected'},
                                  frozenset())
            with self.assertRaisesRegex(RuntimeError, 'same run sentinel'):
                lifecycle.setup(repeat=1, active=2, idle_clients=0, total_clients=2)
            self.assertFalse((data / 'N2-R1').exists())


if __name__ == '__main__':
    unittest.main()
