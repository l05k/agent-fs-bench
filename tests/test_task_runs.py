"""Every Python workload's `task()` runs end to end as one task and emits its tables.

Network, pinned-version and Docker tools are replaced by small scripts; the
workload's own preparation, timing and row code run unchanged. dev-workflow is not
covered: it needs real Linux kernel commits, and its task code matches
mini-dev-workflow's.
"""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import (agent_replay, clone, kvtest, mini_dev_workflow,
                                       npm_install, sqlite_speedtest, sqlite_tools)
from fake_task import run_task
from test_kvtest import KVTEST_RUN_STDOUT
from test_sqlite_speedtest import SPEEDTEST_STDOUT

LINUX = sys.platform.startswith('linux')


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

    @unittest.skipUnless(LINUX and shutil.which('cp'), 'Linux and cp are required')
    def test_clone(self):
        args = clone.parse_args(['--smoke'])
        args.files, args.repeats = 8, 1
        rows = run_task(clone, args)
        self.assertEqual(len(rows['batches']), 1)
        self.assertEqual([(row['sample'], row['host']) for row in rows['requests']],
                         [('0', 'local')])

    def test_kvtest(self):
        args = kvtest.parse_args([])
        args.build_dir, args.repeats = str(self.tools), 1
        sources, build = self.fake_sqlite_build('kvtest', KVTEST_RUN_STDOUT)
        with sources, build:
            rows = run_task(kvtest, args)
        self.assertEqual([(row['tasks'], row['sample'], row['host']) for row in rows['repeats']],
                         [('1', '0', 'local')])

    def test_sqlite_speedtest(self):
        args = sqlite_speedtest.parse_args([])
        args.build_dir, args.repeats = str(self.tools), 1
        sources, build = self.fake_sqlite_build('speedtest1', SPEEDTEST_STDOUT)
        with sources, build:
            rows = run_task(sqlite_speedtest, args)
        self.assertEqual(len(rows['totals']), 1)
        self.assertEqual(len(rows['stages']), len(sqlite_speedtest.MAIN_STAGES))

    @unittest.skipUnless(all(shutil.which(name) for name in
                             ('git', 'make', 'cc', 'bash', 'grep', 'sed', 'python3')),
                         'git, make, a C compiler and the shell tools are required')
    def test_mini_dev_workflow(self):
        rows = run_task(mini_dev_workflow, mini_dev_workflow.parse_args([]))
        self.assertEqual(len(rows['workflow']), 1)
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
        with mock.patch.dict(os.environ, {'PATH': f'{self.tools}:{os.environ["PATH"]}'}):
            rows = run_task(npm_install, args)
        self.assertEqual(len(rows['batches']), 1)
        self.assertEqual([row['host'] for row in rows['installs']], ['local'])

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
            rows = run_task(agent_replay, args)
        self.assertEqual([(row['case'], row['elapsed_s']) for row in rows['replays']],
                         [('smoke', '1.5')])
        self.assertEqual({row['phase'] for row in rows['phases']}, {'agent_execution', 'verifier'})


if __name__ == '__main__':
    unittest.main()
