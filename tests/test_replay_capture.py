"""Keep trace sharding and accepted-cassette installation lossless and fail-fast."""

import io
import os
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from agent_fs_bench import replay_capture as capture
from agent_fs_bench.benchmarks.agent_replay import Mount, ReplayCase, fingerprint
from agent_fs_bench.ssh import Host


class CaptureShardingTests(unittest.TestCase):
    def test_each_task_harness_is_recorded_once_not_once_per_host(self):
        cases = [ReplayCase(f'case-{i}', 'benchmark', str(i // 2),
                            ('codex', 'openclaw')[i % 2], Path('task'), Path(f'cassette-{i}'),
                            (Mount(Path('initial'), '/app'),)) for i in range(378)]
        hosts = tuple(Host(f'node-{i}', f'node-{i}') for i in range(7))
        groups = capture.shards(cases, hosts)
        self.assertEqual({case.name for _, group in groups for case in group},
                         {case.name for case in cases})
        self.assertEqual(sum(len(group) for _, group in groups), len(cases))
        for index, (host, group) in enumerate(groups):
            self.assertEqual(host, hosts[index])
            self.assertEqual(group, cases[index::len(hosts)])
        self.assertEqual(capture.shards(cases[:1], hosts), [(hosts[0], cases[:1])])
        with self.assertRaises(ValueError):
            capture.shards(cases, ())


@unittest.skipUnless(sys.platform == 'linux', 'replay hosts require Linux')
class CassetteInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        task, initial = self.root / 'task', self.root / 'initial'
        task.mkdir()
        initial.mkdir()
        (task / 'task.toml').write_text('version = "1.0"\n')
        self.payload = initial / 'payload'
        self.payload.write_text('pristine')
        self.case = ReplayCase('one', 'smoke', 'one', 'codex', task,
                               self.root / 'accepted', (Mount(initial, '/app'),))
        self.missing = ReplayCase('two', 'smoke', 'two', 'openclaw', task,
                                  self.root / 'missing', (Mount(initial, '/app'),))
        self.stage = self.root / 'stage'
        cassette = self.stage / 'cassettes' / 'one'
        cassette.mkdir(parents=True)
        for name, content in (('manifest.json', '{"schema_version":3}'),
                              ('flows.mitm', 'private-flow'), ('tools.json', '[]')):
            (cassette / name).write_text(content)
        self.expected = fingerprint(cassette)
        self.index = {'source_sha256': 'revision', 'cases': [
            {**capture.identity(self.case), 'status': 'ok',
             'inputs': capture.input_facts(self.case),
             'cassette_sha256': capture.cassette_digest(cassette)},
            {**capture.identity(self.missing), 'status': 'timeout'},
        ]}

    def install(self):
        (self.stage / 'index.json').write_text(json.dumps(self.index))
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as handle:
            for path in self.stage.iterdir():
                handle.add(path, arcname=path.name)
        archive.seek(0)
        with (tempfile.TemporaryFile() as source,
              mock.patch.object(capture, 'PROJECT_ROOT', self.root),
              mock.patch.object(capture, 'source_digest', return_value='revision'),
              mock.patch.object(capture.replay, 'load_cases', return_value=[self.case, self.missing]),
              mock.patch.object(capture.sys, 'stdin', mock.Mock(buffer=source))):
            source.write(archive.getvalue())
            source.seek(0)
            capture.install('cases.toml')

    def test_only_accepted_cassettes_install_and_failed_capture_stays_missing(self):
        self.install()
        self.assertEqual(fingerprint(self.case.cassette), self.expected)
        self.assertFalse(self.missing.cassette.exists())
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            self.install()

    def test_transfer_permissions_do_not_change_cassette_identity(self):
        cassette = self.stage / 'cassettes' / 'one'
        for path in cassette.iterdir():
            path.chmod(0o666)
        previous = os.umask(0o077)
        try:
            self.install()
        finally:
            os.umask(previous)
        self.assertEqual(capture.cassette_digest(self.case.cassette),
                         self.index['cases'][0]['cassette_sha256'])
        self.assertEqual((self.case.cassette / 'flows.mitm').stat().st_mode & 0o777, 0o600)

    def test_changed_initial_data_blocks_all_installation(self):
        self.payload.write_text('different task input')
        with self.assertRaisesRegex(ValueError, 'inputs differ'):
            self.install()
        self.assertFalse(self.case.cassette.exists())

    def test_changed_cassette_blocks_installation(self):
        (self.stage / 'cassettes' / 'one' / 'flows.mitm').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'cassette differs'):
            self.install()
        self.assertFalse(self.case.cassette.exists())


if __name__ == '__main__':
    unittest.main()
