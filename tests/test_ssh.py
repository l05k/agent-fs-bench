"""Inventory ambiguity must not silently choose another MPI worker/account."""

import unittest

from agent_fs_bench.ssh import Host, _select_host


class MpiHostSelectionTests(unittest.TestCase):
    def test_ambiguous_short_hostname_cannot_launch_wrong_worker(self):
        hosts = (Host('one', 'worker.region-a.internal', user='bench'),
                 Host('two', 'worker.region-b.internal', user='bench'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            _select_host(hosts, 'worker', None)
        self.assertEqual(_select_host(hosts, 'worker.region-b.internal', None), hosts[1])

    def test_mpi_cannot_override_an_explicit_inventory_account(self):
        hosts = (Host('one', '10.0.0.8', user='bench', port=2222),)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            _select_host(hosts, 'root@10.0.0.8', None)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            _select_host(hosts, 'bench@10.0.0.8', 'root')
        self.assertEqual(_select_host(hosts, 'bench@10.0.0.8', None), hosts[0])


if __name__ == '__main__':
    unittest.main()
