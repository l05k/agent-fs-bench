"""Placement, pool and job validity of the cluster launcher; no MPI is executed."""

from pathlib import Path
import tempfile
import unittest

from agent_fs_bench import cluster
from agent_fs_bench.cluster import (
    Launcher, Node, Pool, ProgramRun, encode_message, parse_messages, place,
    validate_job, validate_pool,
)
from agent_fs_bench.ssh import Host
from agent_fs_bench.targets import Target

LIBRARY = 'Open MPI v4.1.2, package: Debian OpenMPI, ident: 4.1.2, repo rev: v4.1.2'
LAUNCHER = Launcher(('mpiexec',), (), {}, Path('hosts.toml'), 15)


def node(name, hostname=None, *, machine='x86_64', source='s', library=LIBRARY, system='Linux'):
    host = None if name == 'local' else Host(name, f'{name}.example')
    return Node(name, host, Path('/srv/bench'), '/srv/bench/.venv/bin/python',
                {'hostname': hostname or f'{name}-host', 'machine': machine, 'system': system,
                 'source_sha256': source, 'mpi_library': library})


def placed(placement):
    return [machine.name for machine in placement]


class PlacementTests(unittest.TestCase):
    def test_tasks_are_dealt_round_robin_with_contiguous_ranks(self):
        nodes = [node('local'), node('node-a'), node('node-b')]
        for tasks, expected in (
            (1, ['local']),
            (2, ['local', 'node-a']),
            (7, ['local'] * 3 + ['node-a'] * 2 + ['node-b'] * 2),
        ):
            with self.subTest(tasks=tasks):
                self.assertEqual(placed(place(nodes, tasks)), expected)

    def test_one_machine_takes_any_number_of_tasks(self):
        self.assertEqual(placed(place([node('node-a')], 1024)), ['node-a'] * 1024)

    def test_tasks_must_be_a_positive_integer(self):
        for tasks in (0, -1, None, True, 1.5):
            with self.subTest(tasks=tasks), self.assertRaisesRegex(AssertionError, 'tasks'):
                place([node('local')], tasks)

    def test_appfile_line_is_the_rank_and_declares_the_machine_task_count(self):
        placement = place([node('local'), node('node-a')], 3)
        lines = cluster.appfile(placement, [['p', str(rank)] for rank in range(3)]).splitlines()
        self.assertEqual(lines, [
            '-np 1 --host localhost:2 --wdir /srv/bench p 0',
            '-np 1 --host localhost:2 --wdir /srv/bench p 1',
            '-np 1 --host node-a.example:1 --wdir /srv/bench p 2',
        ])


class PoolTests(unittest.TestCase):
    def pool(self, *nodes, version='4.1.2'):
        return Pool('overflow', nodes, LAUNCHER, version)

    def test_consistent_pool_is_valid(self):
        validate_pool(self.pool(node('local'), node('node-a')), 's')

    def test_invalid_pools_fail_with_expected_and_measured_values(self):
        for pool, message in (
            (self.pool(), 'pool machines'),
            (self.pool(node('local', 'lab-1'), node('node-a', 'lab-1')), 'pool hostnames'),
            (self.pool(node('local'), node('node-a', machine='aarch64')), 'architectures'),
            (self.pool(node('local'), node('node-a', source='old')),
             'node-a source: expected s, measured old'),
            (self.pool(node('local', system='Darwin')), 'local system'),
            # mpi4py silently binds MPICH when it is first on the library path.
            (self.pool(node('local', library='MPICH Version: 4.0.3')), 'mpi4py library'),
            (self.pool(node('local'), version='4.1.6'), 'mpi4py library'),
            (self.pool(node('local'), version='5.0.0'), 'launcher Open MPI'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(AssertionError, message):
                validate_pool(pool, 's')

    def test_launcher_version_reads_open_mpi_and_keeps_other_text(self):
        with tempfile.TemporaryDirectory() as directory:
            for text, expected in (('mpiexec (OpenRTE) 4.1.2', '4.1.2'),
                                   ('HYDRA build details:', 'HYDRA build details:')):
                path = Path(directory) / 'mpiexec'
                path.write_text(f'#!/bin/sh\necho "{text}"\n')
                path.chmod(0o755)
                launcher = Launcher((str(path),), (), {}, Path('hosts.toml'), 15)
                with self.subTest(text=text):
                    self.assertEqual(cluster.launcher_version(launcher), expected)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.placement = place([node('local', 'lab-a'), node('node-b', 'lab-b')], 3)

    def job(self, hostnames, *, done=True, returncode=0):
        stdout = ('Open MPI warning line\n' + encode_message('placement', hostnames)
                  + encode_message('row', ['t', {'k': 1}, {'m': 2.0}])
                  + (encode_message('done', {}) if done else ''))
        return ProgramRun('job', returncode, stdout, Path('job.stderr.txt'))

    def test_complete_job_on_planned_hosts_is_valid_and_keeps_rows(self):
        run = self.job(['lab-a', 'lab-a', 'lab-b'])
        messages = parse_messages(run.stdout)
        validate_job(run, messages, self.placement)
        self.assertEqual(messages.rows, [['t', {'k': 1}, {'m': 2.0}]])

    def test_wrong_host_missing_completion_or_failed_exit_is_invalid(self):
        for run, message in (
            (self.job(['lab-a', 'lab-b', 'lab-b']), 'rank hostnames'),
            (self.job(['lab-a', 'lab-a', 'lab-b'], done=False), 'completion message'),
            (self.job(['lab-a', 'lab-a', 'lab-b'], returncode=1), 'exit status'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(AssertionError, message):
                validate_job(run, parse_messages(run.stdout), self.placement)


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

    def test_unmounted_target_fails_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'missing'
            spec = cluster.JobSpec('m', 'clone', 'run', Target('t', target), None,
                                   (('local', 'host'),))
            task = cluster.Task(self.Comm(), spec)
            with self.assertRaisesRegex(AssertionError, 'target directory'):
                task.setup()
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
