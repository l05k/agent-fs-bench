"""Placement, pool and launch invariants of the cluster dispatcher; no MPI is executed."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench import cluster
from agent_fs_bench.cluster import Node, allocate, check_placement, check_pool
from agent_fs_bench.ssh import Host

LIBRARY = 'Open MPI v4.1.2, package: Debian OpenMPI, ident: 4.1.2, repo rev: v4.1.2'


def node(name, cpus, hostname=None, *, machine='x86_64', source='s', library=LIBRARY,
         root='/srv/bench'):
    host = None if name == 'local' else Host(name, f'{name}.example')
    return Node(name, host, Path(root), '/srv/bench/.venv/bin/python',
                {'hostname': hostname or f'{name}-host', 'cpus': list(cpus), 'machine': machine,
                 'source_sha256': source, 'mpi_library': library})


def placed(slots):
    return [(slot.node.name, slot.cpus) for slot in slots]


class AllocateTests(unittest.TestCase):
    def test_controller_fills_before_hosts_and_keeps_real_cpu_ids(self):
        nodes = [node('local', [7, 2, 11]), node('node-a', [6, 0]), node('node-b', [9, 4])]
        local = [('local', (2,)), ('local', (7,)), ('local', (11,))]
        for tasks, expected in (
            (1, local[:1]),
            (3, local),
            (4, local + [('node-a', (0,))]),
            (6, local + [('node-a', (0,)), ('node-a', (6,)), ('node-b', (4,))]),
        ):
            with self.subTest(tasks=tasks):
                self.assertEqual(placed(allocate(nodes, tasks)), expected)

    def test_none_fills_every_node_and_out_of_range_requests_fail(self):
        nodes = [node('local', [8, 3]), node('node-a', [12])]
        self.assertEqual(placed(allocate(nodes, None)),
                         [('local', (3,)), ('local', (8,)), ('node-a', (12,))])
        for tasks in (0, -1, 4, True, (1, 2)):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                allocate(nodes, tasks)

    def test_multi_cpu_tasks_take_whole_groups_and_skip_too_small_nodes(self):
        nodes = [node('local', range(10)), node('node-a', range(3)), node('node-b', range(4))]
        self.assertEqual(placed(allocate(nodes, None, cpus_per_task=4)),
                         [('local', (0, 1, 2, 3)), ('local', (4, 5, 6, 7)),
                          ('node-b', (0, 1, 2, 3))])
        with self.assertRaisesRegex(ValueError, 'between 1 and 3'):
            allocate(nodes, 4, cpus_per_task=4)

    def test_oversubscription_reuses_each_cpu_group_only_after_all_are_used(self):
        nodes = [node('local', [0, 1]), node('node-a', [5])]
        self.assertEqual(placed(allocate(nodes, None, oversubscribe=2)),
                         [('local', (0,)), ('local', (1,)), ('local', (0,)), ('local', (1,)),
                          ('node-a', (5,)), ('node-a', (5,))])
        # Without oversubscription the same request exceeds capacity instead of shrinking.
        with self.assertRaises(ValueError):
            allocate(nodes, 4)

    def test_slot_shape_parameters_must_be_positive_integers(self):
        for keywords in ({'cpus_per_task': 0}, {'oversubscribe': 0}, {'cpus_per_task': 1.5}):
            with self.subTest(**keywords), self.assertRaises(ValueError):
                allocate([node('local', [0, 1])], 1, **keywords)


class PlacementProofTests(unittest.TestCase):
    def setUp(self):
        self.slots = allocate([node('local', [0, 4], 'lab-a'), node('node-b', [2, 3], 'lab-b')], 3)

    @staticmethod
    def proofs():
        return [{'index': 0, 'hostname': 'lab-a', 'affinity': [0]},
                {'index': 1, 'hostname': 'lab-a', 'affinity': [4]},
                {'index': 2, 'hostname': 'lab-b', 'affinity': [2]}]

    def test_matching_proofs_become_the_manifest_record(self):
        self.assertEqual(check_placement('job', self.proofs(), self.slots), [
            {'index': 0, 'node': 'local', 'hostname': 'lab-a', 'cpus': [0]},
            {'index': 1, 'node': 'local', 'hostname': 'lab-a', 'cpus': [4]},
            {'index': 2, 'node': 'node-b', 'hostname': 'lab-b', 'cpus': [2]},
        ])

    def test_wrong_host_wrong_or_wider_affinity_and_missing_tasks_fail(self):
        for change, message in (
            (lambda proofs: proofs[2].update(hostname='lab-c'), 'executed on lab-c'),
            (lambda proofs: proofs[1].update(affinity=[5]), 'CPU affinity'),
            (lambda proofs: proofs[1].update(affinity=[4, 5]), 'CPU affinity'),
            (lambda proofs: proofs.pop(), 'no placement proof'),
            (lambda proofs: proofs.append(dict(proofs[0])), 'twice'),
            (lambda proofs: proofs.append({'index': 7, 'hostname': 'lab-a', 'affinity': [0]}),
             'unknown'),
        ):
            proofs = self.proofs()
            change(proofs)
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                check_placement('job', proofs, self.slots)


class PoolTests(unittest.TestCase):
    def test_one_machine_listed_twice_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate execution hostnames'):
            check_pool([node('local', [0], 'lab-1'), node('node-a', [0], 'lab-1')], '4.1.2', 's')

    def test_stale_checkout_architecture_or_wrong_mpi_library_is_rejected(self):
        for nodes, message in (
            ([node('local', [0]), node('node-a', [0], source='old')], 'differs from the controller'),
            ([node('local', [0]), node('node-a', [0], machine='aarch64')], 'architecture'),
            # mpi4py silently binds MPICH when it is first on the library path.
            ([node('local', [0]), node('node-a', [0], library='MPICH Version: 4.0.3')],
             'mpi4py loads'),
            ([node('local', [0], library='Open MPI v4.1.6, package: Debian')], 'mpi4py loads'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                check_pool(nodes, '4.1.2', 's')
        check_pool([node('local', [0]), node('node-a', [1])], '4.1.2', 's')


class LauncherTests(unittest.TestCase):
    def launcher(self, version_text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mpiexec'
            path.write_text(f'#!/bin/sh\necho "{version_text}"\n')
            path.chmod(0o755)
            with mock.patch.dict(cluster.CONFIG, {'mpiexec': (str(path),)}):
                return cluster.launcher()

    def test_open_mpi_41_reports_name_and_version_without_paths(self):
        self.assertEqual(self.launcher('mpiexec (OpenRTE) 4.1.2'),
                         {'name': 'mpiexec', 'version': '4.1.2'})

    def test_other_mpi_or_series_is_rejected(self):
        for text in ('HYDRA build details:', 'mpiexec (Open MPI) 5.0.0'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'requires Open MPI 4.1'):
                self.launcher(text)


class AppfileTests(unittest.TestCase):
    def test_tokens_that_open_mpi_would_split_are_rejected(self):
        slots = allocate([node('local', [0], root='/srv/my bench')], 1)
        with self.assertRaisesRegex(ValueError, 'whitespace'):
            cluster.appfile(slots, [['python', '-m', 'agent_fs_bench.cluster']])


class TaskDirectoryTests(unittest.TestCase):
    class Comm:
        """A one-task communicator: collectives are no-ops."""

        def Split(self, color, key):
            return self

        def Get_size(self):
            return 1

        def Get_rank(self):
            return 0

        def Barrier(self):
            pass

    def task(self, target):
        spec = {'slots': [('local', 'host', (0,))], 'args': None, 'benchmark': 'clone',
                'run_id': 'run', 'target': ('t', str(target))}
        return cluster.Task(self.Comm(), spec, 0)

    def test_symlink_cannot_redirect_managed_work_outside_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, outside = root / 'target', root / 'outside'
            target.mkdir()
            outside.mkdir()
            (target / 'agent-fs-bench').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'escapes target'):
                self.task(target).setup()
            self.assertEqual(list(outside.iterdir()), [])

    def test_unmounted_target_fails_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'missing'
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                self.task(target).setup()
            self.assertFalse(target.exists())


class ExecWrapperTests(unittest.TestCase):
    """The exec wrapper's directory handling, driven without MPI."""

    def run_wrapper(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {'OMPI_COMM_WORLD_RANK': '0'}), \
                mock.patch.object(cluster.socket, 'gethostname', return_value='host'), \
                mock.patch.object(cluster.os, 'sched_getaffinity', return_value={0}), \
                mock.patch.object(cluster.os, 'sched_setaffinity'):
            return cluster._exec_main(argv)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux affinity calls are required')
    def test_a_second_task_may_use_a_directory_the_first_task_already_filled(self):
        # Tasks of one machine share mdtest's test directory: the first task's mdtest
        # writes into it before the second wrapper runs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared, capture = root / 'test-dir.0-0', root / 'capture.txt'
            argv = ['--index', '0', '--hostname', 'host', '--cpus', '0',
                    '--mkdir', str(shared), '--capture', str(capture),
                    '--', '/bin/sh', '-c', f'echo csv > {capture}']
            self.assertEqual(self.run_wrapper(argv), 0)
            self.assertTrue(shared.is_dir())
            (shared / 'file').write_text('written by task 0')
            self.assertEqual(self.run_wrapper(argv), 0)
            self.assertEqual((shared / 'file').read_text(), 'written by task 0')


if __name__ == '__main__':
    unittest.main()
