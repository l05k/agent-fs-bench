"""Every Python workload's `task()` runs end to end at N=2 and emits its tables.

Network, pinned-version and Docker tools are replaced by small scripts; the
workload's own preparation, timing, collective and row code run unchanged. Each
workload runs in both placements a real run uses (two tasks on one machine, one task
on each of two machines), because only a multi-task run can show a missing, extra or
reordered collective. dev-workflow is not covered: it needs real Linux kernel commits,
and its task code matches mini-dev-workflow's.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import (agent_replay, clone, kvtest, mini_dev_workflow,
                                       npm_install, sqlite_speedtest, sqlite_tools)
import fake_task
from fake_task import run_tasks
from test_kvtest import KVTEST_RUN_STDOUT
from test_sqlite_speedtest import SPEEDTEST_STDOUT

LINUX = sys.platform.startswith('linux')

LAYOUTS = {
    'one machine': [('local', 'host-a'), ('local', 'host-a')],
    'two machines': [('local', 'host-a'), ('node-a', 'host-b')],
}


def program(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return path


class TaskRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(temporary.cleanup)
        self.tools = Path(temporary.name)

    def fake_sqlite_build(self, tool: str, stdout: str):
        """A prebuilt binary that prints recorded official output."""
        (self.tools / 'stdout.txt').write_text(stdout)
        (self.tools / 'stat.txt').write_text('Integrity-check: ok\n')
        program(self.tools / tool, f'case "$1" in\n'
                f'  run) cat {self.tools}/stdout.txt ;;\n'
                f'  stat) cat {self.tools}/stat.txt ;;\n'
                f'  init) ;;\n'
                f'  *) cat {self.tools}/stdout.txt ;;\nesac\n')
        provenance = {'compiler_version': 'cc 1', 'source_sha256': 's', 'binary_sha256': 'b'}
        return (mock.patch.object(sqlite_tools, 'ensure_sources', return_value={}),
                mock.patch.object(sqlite_tools, 'build',
                                  return_value=(self.tools / tool, provenance)))

    def per_node(self, args, name: str, base: Path | None = None):
        """Args with one `name` directory per machine, as separate machines have."""
        base = self.tools if base is None else base

        def args_for(node):
            copy = argparse.Namespace(**vars(args))
            setattr(copy, name, str(base / node))
            (base / node).mkdir(exist_ok=True)
            return copy
        return args_for

    @unittest.skipUnless(LINUX and shutil.which('cp'), 'Linux and cp are required')
    def test_clone(self):
        args = clone.parse_args(['--smoke'])
        args.files, args.repeats, args.seed = 8, 1, 1
        for label, slots in LAYOUTS.items():
            with self.subTest(layout=label):
                rows = run_tasks(clone, args, slots)
                self.assertEqual(len(rows['batches']), 1)
                self.assertEqual([(row['sample'], row['host']) for row in rows['requests']],
                                 [(str(index), slot[0]) for index, slot in enumerate(slots)])
                self.assertEqual({row['tasks'] for row in rows['batches']}, {'2'})

    def test_kvtest(self):
        args = kvtest.parse_args([])
        args.build_dir, args.repeats = str(self.tools), 1
        sources, build = self.fake_sqlite_build('kvtest', KVTEST_RUN_STDOUT)
        for label, slots in LAYOUTS.items():
            with self.subTest(layout=label):
                with sources, build:
                    rows = run_tasks(kvtest, args, slots)
                self.assertEqual([(row['tasks'], row['sample'], row['host'])
                                  for row in rows['repeats']],
                                 [('2', str(index), slot[0])
                                  for index, slot in enumerate(slots)])

    def test_sqlite_speedtest(self):
        args = sqlite_speedtest.parse_args([])
        args.build_dir, args.repeats = str(self.tools), 1
        sources, build = self.fake_sqlite_build('speedtest1', SPEEDTEST_STDOUT)
        for label, slots in LAYOUTS.items():
            with self.subTest(layout=label):
                with sources, build:
                    rows = run_tasks(sqlite_speedtest, args, slots)
                self.assertEqual(len(rows['totals']), len(slots))
                self.assertEqual(len(rows['stages']),
                                 len(slots) * len(sqlite_speedtest.MAIN_STAGES))

    @unittest.skipUnless(all(shutil.which(name) for name in
                             ('git', 'make', 'cc', 'bash', 'grep', 'sed', 'python3')),
                         'git, make, a C compiler and the shell tools are required')
    def test_mini_dev_workflow(self):
        args = mini_dev_workflow.parse_args([])
        for label, slots in LAYOUTS.items():
            with self.subTest(layout=label):
                rows = run_tasks(mini_dev_workflow, args, slots)
                self.assertEqual(len(rows['workflow']), len(slots))
                self.assertEqual({row['tasks'] for row in rows['workflow']}, {'2'})
                self.assertTrue(rows['steps'])

    @unittest.skipUnless(LINUX and Path('/dev/shm').is_dir(), 'Linux tmpfs /dev/shm is required')
    def test_npm_install(self):
        args = npm_install.parse_args([])
        versions = args.tool_versions
        program(self.tools / 'npm', f'[ "$1" = --version ] && echo {versions["npm"]} && exit 0\n'
                'mkdir -p node_modules/fake && echo x > node_modules/fake/index.js\n')
        program(self.tools / 'node', f'echo {versions["node"]}\n')
        prep = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(prep.cleanup)
        args.prep_dir, args.repeats = prep.name, 1
        # Each machine prepares on its own tmpfs, never on the target's filesystem.
        node_args = self.per_node(args, 'prep_dir', Path(prep.name))
        with mock.patch.dict(os.environ, {'PATH': f'{self.tools}:{os.environ["PATH"]}'}):
            for label, slots in LAYOUTS.items():
                with self.subTest(layout=label):
                    rows = run_tasks(npm_install, args, slots, node_args=node_args)
                    self.assertEqual(len(rows['batches']), 1)
                    self.assertEqual([row['host'] for row in rows['installs']],
                                     [slot[0] for slot in slots])

    @unittest.skipUnless(LINUX, 'GNU cp is required')
    def test_agent_replay(self):
        replay = self.tools / 'replay'
        (replay / 'src').mkdir(parents=True)
        (replay / 'src' / 'replay.py').write_text('')
        for name in ('pyproject.toml', 'uv.lock', 'uv.toml', '.python-version'):
            (replay / name).write_text('')
        program(replay / '.venv' / 'bin' / 'python',
                """echo '{"python": "3.13", "harbor": "0.24.0", "mitmproxy": "12.2.3"}'\n""")
        summary = json.dumps({'mode': 'replay', 'checked': False, 'measured': True,
                              'elapsed_sec': 1.5, 'model_requests': 2,
                              'phase_seconds': {'agent_execution': 1.0, 'verifier': 0.25}})
        program(replay / '.venv' / 'bin' / 'agent-mitm-replay',
                'while [ $# -gt 0 ]; do [ "$1" = --output ] && out=$2; shift; done\n'
                f"mkdir -p \"$out\" && echo '{summary}' > \"$out/summary.json\"\n")
        cases = self.tools / 'cases'
        (cases / 'task').mkdir(parents=True)
        (cases / 'task' / 'task.toml').write_text('')
        cassette = cases / 'cassette'
        cassette.mkdir()
        for name in ('validation.json', 'flows.mitm', 'commands.json', 'task.toml'):
            (cassette / name).write_text('{}')
        (cassette / 'manifest.json').write_text('{"model": "scripted"}')
        (cases / 'initial').mkdir()
        (cases / 'initial' / 'file').write_text('data')
        (cases / 'cases.toml').write_text(
            '[[cases]]\nname = "smoke"\ntask = "task"\ncassette = "cassette"\n'
            '[[cases.mounts]]\nsource = "initial"\ncontainer = "/app"\n')
        args = agent_replay.parse_args([])
        args.cases_file, args.repeats = str(cases / 'cases.toml'), 1
        with mock.patch.object(agent_replay, 'REPLAY_ROOT', replay):
            for label, slots in LAYOUTS.items():
                with self.subTest(layout=label):
                    rows = run_tasks(agent_replay, args, slots)
                    self.assertEqual([(row['case'], row['sample'], row['elapsed_s'])
                                      for row in rows['replays']],
                                     [('smoke', str(index), '1.5')
                                      for index, _slot in enumerate(slots)])
                    self.assertEqual({row['phase'] for row in rows['phases']},
                                     {'agent_execution', 'verifier'})


class FakeWorldTests(unittest.TestCase):
    """The two-task harness must fail, not hang, when collectives do not line up."""

    def run_two(self, body, timeout=1.0):
        world = fake_task.World(2, timeout)
        errors = []

        def run(index):
            world.attach(index)
            try:
                body(world, index)
            except BaseException as error:
                errors.append(error)
                world.abort(error)

        threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return errors

    def test_a_task_that_skips_a_collective_fails_instead_of_hanging(self):
        def body(world, index):
            world.Barrier()
            if index == 0:
                world.Barrier()

        errors = self.run_two(body)
        self.assertTrue(errors)
        self.assertIn('timed out', str(errors[0]))

    def test_different_collectives_in_the_same_slot_are_rejected(self):
        def body(world, index):
            if index == 0:
                world.Barrier()
            else:
                world.gather('x')

        errors = self.run_two(body)
        self.assertTrue(errors)
        self.assertIn('same collectives in the same order', str(errors[0]))

    def test_gather_returns_every_value_on_the_root_only(self):
        world = fake_task.World(2, 5.0)
        seen = {}
        errors = []

        def run(index):
            world.attach(index)
            try:
                seen[index] = world.gather(index)
            except BaseException as error:
                errors.append(error)
                world.abort(error)

        threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(seen, {0: [0, 1], 1: None})


if __name__ == '__main__':
    unittest.main()
