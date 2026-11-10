"""Consumer-visible mount topology invariants; no privileged mounts required."""

import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest

from agent_fs_bench.benchmarks import mount_scale
from agent_fs_bench.benchmarks.mount_scale_mdtest import Mdtest, Phase
from agent_fs_bench.benchmarks.mount_scale_mounts import Layout, Profile, _run
from agent_fs_bench.results import RunContext, Tables, read_table
from agent_fs_bench.targets import Target


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
        return Layout(profile, staging, private, shared, agents,
                      log_dir=root / 'logs', timeout_s=5)

    def test_directory_count_and_agent_count_are_independent(self):
        for agents, directories in ((1, 3), (4, 2), (2, 1)):
            for topology in ('multi-mount', 'shared-directory'):
                with self.subTest(N=agents, X=directories, topology=topology):
                    with tempfile.TemporaryDirectory() as directory:
                        layout = self.layout(directory, topology, agents, directories)
                        self.assertEqual(len(layout.clients), agents)
                        self.assertEqual(len(layout.mountpoints),
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
                Layout(profile, stage, admin / 'escape', [], 1,
                       log_dir=root / 'logs', timeout_s=5)
            self.assertFalse((outside / 'agent-0000').exists())


@unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX daemon behavior')
class LifecycleCommandTests(unittest.TestCase):
    def test_daemon_inheriting_stdio_does_not_hold_command_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / 'daemon.pid'
            log_path = root / 'mount-agent-0000-00'
            command = '''import os, sys, time
from pathlib import Path
pid = os.fork()
if pid == 0:
    time.sleep(10)
    os._exit(0)
Path(sys.argv[1]).write_text(str(pid))
print('client returned', flush=True)
print('client diagnostic', file=sys.stderr, flush=True)
'''
            try:
                _run([sys.executable, '-c', command, str(pid_path)], log_path, 2)
                # The child is still alive with stdout/stderr inherited, but
                # the completed launcher's output is already available.
                os.kill(int(pid_path.read_text()), 0)
                self.assertEqual(log_path.with_suffix('.stdout.txt').read_text(),
                                 'client returned\n')
                self.assertEqual(log_path.with_suffix('.stderr.txt').read_text(),
                                 'client diagnostic\n')
            finally:
                if pid_path.exists():
                    os.kill(int(pid_path.read_text()), signal.SIGTERM)

    def test_nonreturning_command_times_out_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / 'unmount-agent-0000-00'
            command = 'import time; print("waiting", flush=True); time.sleep(10)'
            with self.assertRaisesRegex(RuntimeError, 'command exceeded 0.5s'):
                _run([sys.executable, '-c', command], log_path, 0.5)
            self.assertEqual(log_path.with_suffix('.stdout.txt').read_text(), 'waiting\n')


class NativeTimingTests(unittest.TestCase):
    # Actual mdtest 4.0.0 output from an N=2 run; aggregate and rank layouts
    # differ by the empty items column. Do not reconstruct fixtures from parser constants.
    CSV = '''rank,items,rate-Directory creation,time-Directory creation,rate-Directory stat,time-Directory stat,rate-Directory read,time-Directory read,rate-Directory rename,time-Directory rename,rate-Directory removal,time-Directory removal,rate-File creation,time-File creation,rate-File stat,time-File stat,rate-File read,time-File read,rate-File removal,time-File removal,rate-Tree creation,time-Tree creation,rate-Tree removal,time-Tree removal
all,32,,,,,,,,,,,4.9618383734e+04,1.2898445129e-03,1.1046726584e+06,5.7935714722e-05,3.5554365033e+05,1.8000602722e-04,1.5063718070e+05,4.2486190796e-04,1.2826617737e+04,1.5592575073e-04,3.5098778243e+04,5.6982040405e-05
0,,,,,,,,,,,,2.4887396254e+04,1.2857913971e-03,5.5233632922e+05,5.7935714722e-05,1.8798001120e+05,1.7023086548e-04,7.5488035996e+04,4.2390823364e-04
1,,,,,,,,,,,,3.3026015748e+04,9.6893310547e-04,5.5233632922e+05,5.7935714722e-05,1.7871867909e+05,1.7905235291e-04,7.7492914550e+04,4.1294097900e-04
'''
    BANNER = 'mdtest-4.0.0 was launched with 2 total task(s) on 1 node(s)\n'

    def parse(self, csv, banner=None, nodes=1):
        return Mdtest._parse('private', self.BANNER if banner is None else banner,
                             csv, ('File creation',), 2, nodes)

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

    def test_banner_node_count_must_match_the_rank_hosts(self):
        # Two ranks on two hosts must not silently accept a one-node banner.
        multi = self.BANNER.replace('1 node(s)', '2 node(s)')
        self.assertEqual(self.parse(self.CSV, multi, nodes=2)[1], 32)
        with self.assertRaisesRegex(RuntimeError, r'expected 2 on 2'):
            self.parse(self.CSV, nodes=2)

    def test_launcher_output_interleaved_with_the_banner_is_tolerated(self):
        banner = '[node-a:1234] ' + self.BANNER
        self.assertEqual(self.parse(self.CSV, banner)[1], 32)

class TableEmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.ctx = RunContext('mount-scale', 'run', Target('fs', self.root),
                              self.root / 'work', self.root / 'res', Tables(self.root))

    def tearDown(self):
        self.ctx.tables.close()
        self.directory.cleanup()

    @staticmethod
    def levels() -> dict:
        return {'used_bytes': 10, 'cached_bytes': 2, 'dirty_bytes': 0, 'slab_bytes': 3,
                'slab_reclaimable_bytes': 1, 'slab_unreclaimable_bytes': 2,
                'mount_entries': 4, 'sample_elapsed_s': 1.0, 'cpu_busy_s': 0.5,
                'cpu_busy_cores': 0.5, 'cpu_busy_percent': 50.0}

    def test_host_samples_become_lifecycle_agent_and_resource_rows(self):
        mount = {'mount_elapsed_s': 0.2, 'measured_mounts': 3,
                 'agent_elapsed_s': (0.1, 0.2)}
        unmount = {'unmount_elapsed_s': 0.1, 'remaining_mounts': 0,
                   'agent_elapsed_s': (0.05, 0.05)}
        workload = {'sample_elapsed_s': 2.0, 'cpu_busy_s': 1.0, 'cpu_busy_cores': 0.5,
                    'cpu_busy_percent': 50.0, 'peak_used_bytes': 20,
                    'peak_used_bytes_delta': 10}
        mount_scale._emit_host(self.ctx, 'multi-mount', 2, 'local', 1, self.levels(),
                               self.levels(), self.levels(), workload, mount, unmount)
        lifecycle = read_table(self.root / 'lifecycle.csv')
        self.assertEqual(
            [(row['action'], row['elapsed_s'], row['measured_mounts'], row['remaining_mounts'])
             for row in lifecycle],
            [('mount', '0.2', '3', ''), ('unmount', '0.1', '', '0')])
        agents = read_table(self.root / 'agents.csv')
        self.assertEqual([(row['action'], row['sample'], row['elapsed_s']) for row in agents],
                         [('mount', '0', '0.1'), ('mount', '1', '0.2'),
                          ('unmount', '0', '0.05'), ('unmount', '1', '0.05')])
        resources = read_table(self.root / 'resources.csv')
        self.assertEqual([row['state'] for row in resources],
                         ['baseline', 'mounted-idle', 'released-idle', 'workload'])
        self.assertEqual(resources[0]['used_bytes_delta'], '')
        self.assertEqual(resources[1]['used_bytes_delta'], '0')
        self.assertEqual(resources[3]['used_bytes'], '')
        self.assertEqual(resources[3]['peak_used_bytes'], '20')
        self.assertEqual(resources[3]['peak_used_bytes_delta'], '10')

    def test_phase_rows_and_rank_samples_share_the_phase_identity(self):
        phases = [Phase('private/create', 4, 0.5, [0.2, 0.3]),
                  Phase('private/stat', 4, 0.25, [0.1, 0.15])]
        mapping = [{'host': 'local', 'cpu': 0}, {'host': 'node-a', 'cpu': 4}]
        mount_scale._emit_phases(self.ctx, 'multi-mount', 2, 1, phases, mapping, 4096)
        rows = read_table(self.root / 'phases.csv')
        self.assertEqual([row['phase'] for row in rows], ['private/create', 'private/stat'])
        self.assertEqual(rows[0]['bytes'], str(4 * 4096))
        self.assertEqual(rows[1]['bytes'], '')
        ranks = read_table(self.root / 'ranks.csv')
        self.assertEqual(
            [(row['phase'], row['sample'], row['host'], row['cpu'], row['elapsed_s'])
             for row in ranks],
            [('private/create', '0', 'local', '0', '0.2'),
             ('private/create', '1', 'node-a', '4', '0.3'),
             ('private/stat', '0', 'local', '0', '0.1'),
             ('private/stat', '1', 'node-a', '4', '0.15')])

if __name__ == '__main__':
    unittest.main()
