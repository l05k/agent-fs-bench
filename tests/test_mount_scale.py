"""Consumer-visible mount topology invariants; no privileged mounts required."""

from pathlib import Path
import tempfile
import unittest

from agent_fs_bench.benchmarks.mount_scale_mdtest import Mdtest
from agent_fs_bench.benchmarks.mount_scale_mounts import Layout, Profile


class MountTopologyTests(unittest.TestCase):
    def layout(self, directory: str, topology: str, agents: int, directories: int) -> Layout:
        root = Path(directory)
        admin, staging = root / 'admin', root / 'staging'
        admin.mkdir()
        staging.mkdir()
        private = admin / 'private'
        private.mkdir()
        shared = [admin / f'shared-{i}' for i in range(1, directories)]
        for path in shared:
            path.mkdir()
        profile = Profile(topology, admin, ('mount', '{subdir}', '{mountpoint}'),
                          ('mount', '{source}', '{mountpoint}'), ('umount', '{mountpoint}'))
        return Layout(profile, staging, private, shared, agents)

    def test_directory_count_and_agent_count_are_independent(self):
        for agents, directories in ((1, 3), (4, 2), (2, 1)):
            for topology in ('multi-mount', 'shared-directory'):
                with self.subTest(N=agents, X=directories, topology=topology):
                    with tempfile.TemporaryDirectory() as directory:
                        layout = self.layout(directory, topology, agents, directories)
                        self.assertEqual(len(layout.clients), agents)
                        self.assertEqual(layout.expected_mounts,
                                         agents * (directories if topology == 'multi-mount' else 1))
                        self.assertEqual(len({client.workspace for client in layout.clients}), agents)
                        for index, client in enumerate(layout.clients):
                            self.assertEqual(len(client.shared), directories - 1)
                            self.assertTrue(client.workspace.parent.name == f'agent-{index:04d}')
                            for number in range(1, directories):
                                # LakeBase's bind target must be absent in its volume.
                                source_target = layout.data_root / f'agent-{index:04d}' / f'shared-{number}'
                                self.assertFalse(source_target.exists())

    def test_admin_symlink_cannot_redirect_private_data_outside_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            admin, outside, stage = (root / name for name in ('admin', 'outside', 'stage'))
            for path in (admin, outside, stage):
                path.mkdir()
            (admin / 'escape').symlink_to(outside, target_is_directory=True)
            profile = Profile('shared-directory', admin, ('mount',), (), ('umount',))
            with self.assertRaisesRegex(ValueError, 'under admin_root'):
                Layout(profile, stage, admin / 'escape', [], 1)
            self.assertFalse((outside / 'agent-0000').exists())


class NativeTimingTests(unittest.TestCase):
    # Actual mdtest 4.0.0 output from an N=2 run; aggregate and rank layouts
    # differ by the empty items column. Do not reconstruct fixtures from parser constants.
    CSV = '''rank,items,rate-Directory creation,time-Directory creation,rate-Directory stat,time-Directory stat,rate-Directory read,time-Directory read,rate-Directory rename,time-Directory rename,rate-Directory removal,time-Directory removal,rate-File creation,time-File creation,rate-File stat,time-File stat,rate-File read,time-File read,rate-File removal,time-File removal,rate-Tree creation,time-Tree creation,rate-Tree removal,time-Tree removal
all,32,,,,,,,,,,,4.9618383734e+04,1.2898445129e-03,1.1046726584e+06,5.7935714722e-05,3.5554365033e+05,1.8000602722e-04,1.5063718070e+05,4.2486190796e-04,1.2826617737e+04,1.5592575073e-04,3.5098778243e+04,5.6982040405e-05
0,,,,,,,,,,,,2.4887396254e+04,1.2857913971e-03,5.5233632922e+05,5.7935714722e-05,1.8798001120e+05,1.7023086548e-04,7.5488035996e+04,4.2390823364e-04
1,,,,,,,,,,,,3.3026015748e+04,9.6893310547e-04,5.5233632922e+05,5.7935714722e-05,1.7871867909e+05,1.7905235291e-04,7.7492914550e+04,4.1294097900e-04
'''
    BANNER = 'mdtest-4.0.0 was launched with 2 total task(s) on 1 node(s)\n'

    def parse(self, csv):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.csv'
            path.write_text(csv)
            return Mdtest._parse('private', self.BANNER, path, ('File creation',), 2)

    def test_aggregate_uses_barrier_time_not_average_rank_time(self):
        _, items, times, ranks = self.parse(self.CSV)
        self.assertEqual(items, 32)
        self.assertEqual(times['File creation'], 1.2898445129e-03)
        self.assertEqual(ranks['File creation'], [1.2857913971e-03, 9.6893310547e-04])
        self.assertGreater(times['File creation'], max(ranks['File creation']))

    def test_missing_client_measurement_is_fatal(self):
        incomplete = '\n'.join(self.CSV.splitlines()[:-1]) + '\n'
        with self.assertRaisesRegex(RuntimeError, 'rank rows'):
            self.parse(incomplete)

    def test_nonfinite_native_phase_cannot_be_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'no time'):
            self.parse(self.CSV.replace('1.2898445129e-03', 'nan'))


if __name__ == '__main__':
    unittest.main()
