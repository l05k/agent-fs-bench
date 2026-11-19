"""Budget cgroup hooks; mocked kernel files do not prove a real site setup."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import mount_scale_budget as budget
from agent_fs_bench.benchmarks import mount_scale_mounts
from agent_fs_bench.benchmarks.mount_scale_mounts import Layout, Profile, parse_profile

ROOT = Path('/sys/fs/cgroup')
PATHS = (ROOT / 'agent-fs-bench' / 'aaaa', ROOT / 'agent-fs-bench' / 'bbbb')


def proof(rank: int, path: Path | None = None, **values) -> str:
    path = PATHS[rank] if path is None else path
    value = {
        'cgroup': str(path),
        'pid': 100 + rank,
        'rank': rank,
        'cpu_max': budget.CPU_MAX,
        'memory_max': budget.MEMORY_MAX,
        'members': [100 + rank, 999],
    }
    value.update(values)
    return budget.PREFIX + json.dumps(value)


class PlacementProofTests(unittest.TestCase):
    def test_proofs_are_returned_in_rank_order(self):
        stdout = '\n'.join((proof(1), 'unrelated launcher output', proof(0)))
        self.assertEqual([value['rank'] for value in
                          budget.read_placements(stdout, PATHS)], [0, 1])

    def test_missing_configured_rank_is_fatal(self):
        with self.assertRaisesRegex(AssertionError, r'missing configured ranks \[1\]'):
            budget.read_placements(proof(0), PATHS)

    def test_duplicate_rank_is_fatal(self):
        with self.assertRaisesRegex(AssertionError, 'duplicate'):
            budget.read_placements('\n'.join((proof(0), proof(0))), PATHS)

    def test_wrong_group_is_fatal(self):
        with self.assertRaisesRegex(AssertionError, 'cgroup: expected'):
            budget.read_placements(proof(0, PATHS[1]), PATHS)

    def test_invalid_limits_are_fatal(self):
        for values in ({'cpu_max': 'max'}, {'memory_max': 0}):
            with self.subTest(values=values), \
                    self.assertRaisesRegex(AssertionError, 'expected'):
                budget.read_placements(proof(0, **values), (PATHS[0],))

    def test_pid_must_be_an_observed_member(self):
        with self.assertRaisesRegex(AssertionError, 'members: expected own pid'):
            budget.read_placements(proof(0, members=[999]), (PATHS[0],))

    def test_unconfigured_rank_proof_is_fatal(self):
        with self.assertRaisesRegex(AssertionError, 'configured ranks'):
            budget.read_placements(proof(0), (None, PATHS[1]))


class KernelJoinTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.group = root / 'agent-fs-bench' / 'abc'
        self.group.mkdir(parents=True)
        (self.group / 'cpu.max').write_text(budget.CPU_MAX + '\n')
        (self.group / 'memory.max').write_text(f'{budget.MEMORY_MAX}\n')
        (self.group / 'cgroup.procs').write_text('')
        self.proc = root / 'proc-self-cgroup'
        self.proc.write_text('0::/agent-fs-bench/abc\n')
        for patch in (mock.patch.object(budget, 'CGROUP_ROOT', root),
                      mock.patch.object(budget, 'PROC_CGROUP', self.proc)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_join_writes_own_pid_and_reports_the_group(self):
        value = budget.join(self.group)
        self.assertEqual(value, {
            'cgroup': str(self.group), 'pid': os.getpid(),
            'cpu_max': budget.CPU_MAX, 'memory_max': budget.MEMORY_MAX,
            'members': [os.getpid()],
        })
        self.assertEqual((self.group / 'cgroup.procs').read_text(),
                         f'{os.getpid()}\n')

    def test_join_rejects_a_different_kernel_membership(self):
        self.proc.write_text('0::/agent-fs-bench/other\n')
        with self.assertRaisesRegex(AssertionError, '0::'):
            budget.join(self.group)

    def test_join_rejects_a_wrong_cpu_limit_before_writing_pid(self):
        (self.group / 'cpu.max').write_text('max\n')
        with self.assertRaisesRegex(AssertionError, 'cpu.max'):
            budget.join(self.group)
        self.assertEqual((self.group / 'cgroup.procs').read_text(), '')

    def test_join_rejects_a_group_outside_the_controller_root(self):
        with self.assertRaisesRegex(ValueError, 'under'):
            budget.join(Path('/outside-cgroup'))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.group = Path(self.directory.name) / 'group'
        self.group.mkdir()
        (self.group / 'cpu.max').write_text(budget.CPU_MAX + '\n')
        (self.group / 'memory.max').write_text(f'{budget.MEMORY_MAX}\n')
        (self.group / 'cpu.stat').write_text(
            'usage_usec 10\nnr_periods 4\nnr_throttled 2\nthrottled_usec 7\n')
        (self.group / 'memory.events').write_text(
            'low 0\nhigh 0\nmax 3\noom 1\noom_kill 0\n')

    def test_snapshot_reports_only_the_selected_kernel_counters(self):
        self.assertEqual(budget.snapshot(self.group), {
            'cgroup': str(self.group),
            'cpu_nr_periods': 4, 'cpu_nr_throttled': 2, 'cpu_throttled_usec': 7,
            'memory_max_events': 3, 'memory_oom': 1, 'memory_oom_kill': 0,
        })

    def test_snapshot_rejects_an_unexpected_cpu_limit(self):
        (self.group / 'cpu.max').write_text('50000 100000\n')
        with self.assertRaisesRegex(AssertionError, 'cpu.max'):
            budget.snapshot(self.group)

    def test_snapshot_rejects_an_unlimited_memory_limit(self):
        (self.group / 'memory.max').write_text('max\n')
        with self.assertRaisesRegex(AssertionError, 'memory.max'):
            budget.snapshot(self.group)


class ProfileBudgetTests(unittest.TestCase):
    def parse(self, key: str) -> Profile:
        return parse_profile(f'''[profiles.fs]
topology = "shared-directory"
admin_root = "/admin"
private_mount = ["mount"]
unmount = ["umount"]
{key}
''', 'fs')

    def test_invalid_rank_cgroup_templates_are_rejected(self):
        values = (
            'true',
            '""',
            '"agent-fs-bench/{mount_hash}"',
            '"/sys/fs/cgroup"',
            '"/tmp/{mount_hash}"',
            '"/sys/fs/cgroup/agent-fs-bench/fixed"',
            '"/sys/fs/cgroup/agent-fs-bench/{unknown}"',
            '"/sys/fs/cgroup/agent-fs-bench/{mount_hash}/{mount_hash}"',
            '"/sys/fs/cgroup/agent-fs-bench/{mount_hash:>20}"',
            '"/sys/fs/cgroup/agent-fs-bench/{mount_hash!r}"',
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(f'rank_cgroup = {value}')


class LayoutBudgetTests(unittest.TestCase):
    def layout(self, root: Path, template: str) -> Layout:
        admin, staging, data = root / 'admin', root / 'staging', root / 'admin' / 'data'
        for path in (admin, staging, data):
            path.mkdir(exist_ok=True)
        profile = Profile('multi-mount', admin, ('mount',), ('umount',), template)
        return Layout(profile, staging, data, 2, log_dir=root / 'logs', timeout_s=5)

    def test_unmount_rejects_a_group_left_by_the_site_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self.layout(root, str(root / 'cgroups' / '{mount_hash}'))
            group = layout.clients[0].cgroup
            group.mkdir(parents=True)
            layout._mounted = True
            with mock.patch.object(mount_scale_mounts, '_mount_points', return_value={}), \
                    mock.patch.object(Layout, '_time', return_value=(0.0, (0.0,))):
                with self.assertRaisesRegex(RuntimeError, 'budget cgroup.*remain'):
                    layout.unmount()
                self.assertTrue(layout._mounted)
                group.rmdir()
                self.assertEqual(layout.unmount()['remaining_mounts'], 0)
            self.assertFalse(layout._mounted)


class JoinFailureProcessTests(unittest.TestCase):
    def test_failed_join_never_execs_the_native_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            helper = Path(directory) / 'helper.py'
            helper.write_text("import sys\nfrom pathlib import Path\n"
                              "Path(sys.argv[1]).write_text('executed')\n")
            group = (f'/sys/fs/cgroup/agent-fs-bench/'
                     f'absent-{secrets.token_hex(8)}')
            program = (
                'import sys\n'
                'from agent_fs_bench.benchmarks.mount_scale_runner import mdtest_main\n'
                f'mdtest_main({["--cgroup", group, "--", sys.executable, str(helper),
                                 str(marker)]!r})\n'
            )
            completed = subprocess.run(
                [sys.executable, '-c', program],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'OMPI_COMM_WORLD_RANK': '0'},
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertNotIn(budget.PREFIX, completed.stdout)


if __name__ == '__main__':
    unittest.main()
