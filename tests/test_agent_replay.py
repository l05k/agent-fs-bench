import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import agent_replay
from agent_fs_bench.benchmarks.agent_replay import fingerprint, load_cases, restore
from agent_fs_bench.results import RunContext, Tables, read_table
from agent_fs_bench.targets import Target

class ReplayInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'initial'
        self.source.mkdir()
        self.task = self.root / 'task'
        self.task.mkdir()
        (self.task / 'task.toml').write_text('version = "1.0"\n')
        self.cassette = self.root / 'cassette'
        self.cassette.mkdir()
        self.config = self.root / 'cases.toml'
        self.config.write_text(
            '[[cases]]\nname = "task-a"\ntask = "task"\ncassette = "cassette"\n'
            '[[cases.mounts]]\nsource = "initial"\ncontainer = "/app"\n'
        )
        self.replay_root = self.root / 'replay'
        self.bin_dir = self.replay_root / '.venv' / 'bin'

    def install_replay_environment(self):
        (self.cassette / 'validation.json').write_text('{}')
        (self.cassette / 'manifest.json').write_text('{"model": "openai/test"}')
        for name in ('flows.mitm', 'commands.json', 'task.toml'):
            (self.cassette / name).touch()
        self.bin_dir.mkdir(parents=True)
        (self.bin_dir / 'agent-mitm-replay').touch()
        for name in ('pyproject.toml', 'uv.lock', 'uv.toml', '.python-version'):
            (self.replay_root / name).touch()

    def subprocess_run(self, summary=None):
        real_run = subprocess.run

        def run_command(command, **kwargs):
            if command[0] == str(self.bin_dir / 'python'):
                return subprocess.CompletedProcess(command, 0, stdout='{}')
            if summary is not None and command[0] == str(self.bin_dir / 'agent-mitm-replay'):
                output = Path(command[command.index('--output') + 1])
                output.mkdir()
                (output / 'summary.json').write_text(json.dumps(summary))
                return subprocess.CompletedProcess(command, 0)
            if command[0] == 'cp':
                return real_run(command, **kwargs)
            self.fail('replay started with incorrect initial data')

        return run_command

    def test_unaccepted_cassette_cannot_become_a_performance_sample(self):
        with self.assertRaisesRegex(ValueError, 'validate cassette'):
            load_cases(self.config)
        (self.cassette / 'validation.json').write_text(json.dumps({'minimum_reward': 1.0}))
        case, = load_cases(self.config)
        self.assertEqual(case.mounts[0].source, self.source.resolve())

    def test_overlapping_mounts_cannot_hide_restored_data(self):
        (self.cassette / 'validation.json').write_text('{}')
        with self.config.open('a') as handle:
            handle.write('[[cases.mounts]]\nsource = "initial"\ncontainer = "/app/data"\n')
        with self.assertRaisesRegex(ValueError, 'overlapping container mounts'):
            load_cases(self.config)

    def test_content_modes_and_links_distinguish_experiment_inputs(self):
        payload = self.source / '.hidden'
        payload.write_bytes(b'original')
        payload.chmod(0o644)
        (self.source / 'link').symlink_to('.hidden')
        original = fingerprint(self.source)
        payload.write_bytes(b'modified')
        self.assertNotEqual(fingerprint(self.source), original)
        payload.write_bytes(b'original')
        payload.chmod(0o755)
        self.assertNotEqual(fingerprint(self.source), original)
        payload.chmod(0o644)
        (self.source / 'link').unlink()
        (self.source / 'link').symlink_to('missing')
        self.assertNotEqual(fingerprint(self.source), original)

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('cp'), 'GNU cp on Linux required')
    def test_each_restore_preserves_tree_semantics_without_reusing_mutations(self):
        payload = self.source / '.hidden'
        payload.write_bytes(b'initial')
        payload.chmod(0o755)
        os.link(payload, self.source / 'hardlink')
        (self.source / 'symlink').symlink_to('.hidden')
        (self.source / 'empty').mkdir()
        first, second = self.root / 'first', self.root / 'second'
        expected = fingerprint(self.source)
        restore(self.source, first)
        self.assertEqual(fingerprint(first), expected)
        (first / '.hidden').write_bytes(b'changed by task')
        (first / 'created-by-task').touch()
        restore(self.source, second)
        self.assertEqual(fingerprint(second), expected)
        self.assertEqual((first / 'hardlink').read_bytes(), b'changed by task')
        self.assertEqual((second / '.hidden').read_bytes(), b'initial')
        self.assertFalse((second / 'created-by-task').exists())
        self.assertEqual(payload.read_bytes(), b'initial')

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('cp'), 'GNU cp on Linux required')
    def test_restored_metadata_loss_blocks_replay_and_measurement(self):
        payload = self.source / 'payload'
        payload.write_bytes(b'initial')
        payload.chmod(0o755)
        os.link(payload, self.source / 'hardlink')
        with self.config.open('a') as handle:
            handle.write('[[cases.mounts]]\nsource = "initial"\ncontainer = "/data"\n')
        self.install_replay_environment()
        for corruption in ('mode', 'hardlink'):
            with self.subTest(corruption=corruption):
                target = self.root / f'target-{corruption}'
                work = target / 'work'
                work.mkdir(parents=True)
                results = self.root / f'results-{corruption}'
                results.mkdir()
                ctx = RunContext('agent-replay', 'run', Target('fs', target),
                                 work, results, Tables(results))

                def restore_with_loss(source, destination):
                    restore(source, destination)
                    if destination.name == 'mount-1':
                        if corruption == 'mode':
                            (destination / 'payload').chmod(0o644)
                        else:
                            link = destination / 'hardlink'
                            link.unlink()
                            link.write_bytes(b'initial')
                            link.chmod(0o755)

                with (
                    mock.patch.object(agent_replay, 'REPLAY_ROOT', self.replay_root),
                    mock.patch.object(agent_replay, 'restore', side_effect=restore_with_loss),
                    mock.patch.object(subprocess, 'run', side_effect=self.subprocess_run()),
                    self.assertRaisesRegex(RuntimeError, 'restored task data differs from source'),
                ):
                    agent_replay.run(ctx, argparse.Namespace(cases_file=self.config, repeats=1))
                self.assertEqual(ctx.rows, 0)
                self.assertEqual(list(results.glob('*.csv')), [])

    def test_measured_replay_writes_tables_and_case_facts(self):
        self.install_replay_environment()
        summary = {'mode': 'replay', 'checked': False, 'measured': True,
                   'elapsed_sec': 12.5, 'model_requests': 3,
                   'phase_seconds': {'environment_setup': 1.5,
                                     'agent_execution': 10.0, 'verifier': None}}
        target = self.root / 'target'
        work = target / 'work'
        work.mkdir(parents=True)
        results = self.root / 'results'
        results.mkdir()
        tables = Tables(results)
        ctx = RunContext('agent-replay', 'run', Target('fs', target), work, results, tables)
        with (
            mock.patch.object(agent_replay.sys, 'platform', 'linux'),
            mock.patch.object(agent_replay, 'REPLAY_ROOT', self.replay_root),
            mock.patch.object(agent_replay, 'restore', side_effect=shutil.copytree),
            mock.patch.object(subprocess, 'run', side_effect=self.subprocess_run(summary)),
        ):
            agent_replay.run(ctx, argparse.Namespace(cases_file=self.config, repeats=2))
        tables.close()
        replays = read_table(results / 'replays.csv')
        self.assertEqual([(row['case'], row['repeat'], row['elapsed_s'], row['model_requests'])
                          for row in replays],
                         [('task-a', '1', '12.5', '3'), ('task-a', '2', '12.5', '3')])
        phases = read_table(results / 'phases.csv')
        self.assertEqual([(row['repeat'], row['phase'], row['seconds']) for row in phases],
                         [('1', 'environment_setup', '1.5'), ('1', 'agent_execution', '10.0'),
                          ('2', 'environment_setup', '1.5'), ('2', 'agent_execution', '10.0')])
        self.assertEqual(ctx.rows, 6)
        self.assertEqual(ctx.info['cases']['task-a']['model'], 'openai/test')
        self.assertEqual(ctx.info['cases']['task-a']['mounts'][0]['container'], '/app')

if __name__ == '__main__':
    unittest.main()
