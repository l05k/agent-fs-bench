import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import dev_workflow, mini_dev_workflow
from agent_fs_bench.benchmarks.build_settings import read_compiler_version
from agent_fs_bench.results import RunContext
from agent_fs_bench.targets import Target

BUILD_VARIABLES = (
    'CC', 'CXX', 'CPP', 'AR', 'LD', 'AS', 'NM', 'STRIP', 'OBJCOPY',
    'CFLAGS', 'CPPFLAGS', 'LDFLAGS', 'LDLIBS', 'HOSTCC', 'HOSTCXX',
    'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES', 'MAKEFILES', 'MAKELEVEL',
    'ARCH', 'SRCARCH', 'CROSS_COMPILE', 'LLVM', 'LLVM_IAS',
    'KCFLAGS', 'KAFLAGS', 'KCONFIG_CONFIG', 'KBUILD_OUTPUT',
    'BUILD_OBSERVATIONS',
)


class WorkflowRecordingTests(unittest.TestCase):
    def setUp(self):
        if not all(shutil.which(name) for name in ('make', 'cc', 'git', 'bash')):
            self.skipTest('GNU make, a C compiler, Git and Bash are required')
        version = subprocess.run(['make', '--version'], capture_output=True, text=True, check=True)
        if 'GNU Make' not in version.stdout:
            self.skipTest('GNU make is required')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if key not in BUILD_VARIABLES and not key.startswith('GIT_')}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))

    def context(self, name):
        work = self.root / name
        work.mkdir()
        return RunContext(name, 'run', Target('local', self.root), work, work,
                          {}, 'source', io.StringIO())

    def compiler_version(self, command='cc'):
        output = subprocess.run([command, '--version'], capture_output=True, text=True,
                                check=True).stdout
        return output.strip().splitlines()[0]

    def git(self, repo, *args):
        return subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
             '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *args],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def kernel_fixture(self):
        source = self.root / 'source'
        source.mkdir()
        self.git(source, 'init', '--quiet')
        (source / 'Makefile').write_text(
            '.DEFAULT_GOAL := all\n'
            'CC = cc\n'
            '.PHONY: all probe tinyconfig olddefconfig\n'
            'all: probe\n\t@./probe >> "$(BUILD_OBSERVATIONS)"\n'
            'probe: probe.c\n'
            '\t$(CC) $(KCFLAGS) probe.c -o probe\n'
            'tinyconfig:\n'
            '\t@printf \'%s\\n\' \'# CONFIG_OVERLAY_FS is not set\' > .config\n'
            'olddefconfig:\n'
            '\t@test -f .config\n'
        )
        before = '#include <stdio.h>\nint main(void) { printf("before:%d\\n", BENCH_VALUE); return 0; }\n'
        after = before.replace('before:', 'after:')
        (source / 'probe.c').write_text(before)
        self.git(source, 'add', '.')
        self.git(source, 'commit', '--quiet', '-m', 'base')
        base = self.git(source, 'rev-parse', 'HEAD')
        (source / 'probe.c').write_text(after)
        self.git(source, 'add', '.')
        self.git(source, 'commit', '--quiet', '-m', 'change message')
        commit = self.git(source, 'rev-parse', 'HEAD')
        fixture = self.root / 'fixture'
        fixture.mkdir()
        (fixture / dev_workflow.DEFAULT_CONFIG).write_text('CONFIG_OVERLAY_FS=y\n')
        (fixture / 'search.cmds').write_text('grep before probe.c\n')
        (fixture / 'read.cmds').write_text('cat probe.c\n')
        (fixture / 'edit.cmds').write_text(
            'python3 -c \'from pathlib import Path; p = Path("probe.c"); '
            'p.write_text(p.read_text().replace("before:", "after:"))\'\n'
        )
        (fixture / dev_workflow.DEFAULT_SERIES).write_text(json.dumps({
            'base_commit': base,
            'commits': [{'id': commit, 'message': 'change message', 'files': ['probe.c'],
                         'search_commands': 'search.cmds', 'read_commands': 'read.cmds',
                         'edit_commands': 'edit.cmds'}],
        }))
        return source, fixture, base

    def test_kernel_replay_preserves_make_precedence(self):
        source, fixture, base = self.kernel_fixture()
        observations = self.root / 'observations'
        os.environ.update(CC='/missing/environment-compiler',
                          KCFLAGS='-DBENCH_VALUE=7',
                          BUILD_OBSERVATIONS=str(observations))
        args = dev_workflow.parse_args([])
        args.fixture_dir, args.linux_url, args.jobs = str(fixture), str(source), 1
        ctx = self.context('kernel')
        # Platform and installed kernel prerequisites are not under test; the
        # checkout, make/compiler, replay, cleanup and JSONL are real.
        with mock.patch.object(dev_workflow, 'require_linux'), \
                mock.patch.object(dev_workflow, 'ensure_dependencies'), \
                contextlib.redirect_stderr(io.StringIO()):
            dev_workflow.run(ctx, args)
        record = json.loads(ctx.output.getvalue())
        self.assertEqual(observations.read_text().splitlines(), ['before:7', 'after:7'])
        self.assertEqual(record['parameters']['base_commit'], base)
        self.assertEqual(record['parameters']['tool_versions']['compiler'],
                         self.compiler_version())
        self.assertEqual(len(record['details']['tree_assertions']), 1)
        self.assertTrue(record['details']['tree_assertions'][0]['ok'])
        self.assertEqual(self.git(ctx.work_dir / 'repo', 'status', '--porcelain'), '')

    def test_mini_records_compiler_selected_by_make(self):
        os.environ.update(CC='/missing/environment-compiler', MAKEFLAGS='CC=cc', CFLAGS='-O0')
        ctx = self.context('mini')
        with mock.patch.object(mini_dev_workflow, 'require_linux'), \
                contextlib.redirect_stderr(io.StringIO()):
            mini_dev_workflow.run(ctx, mini_dev_workflow.parse_args([]))
        record = json.loads(ctx.output.getvalue())
        self.assertEqual(record['parameters']['tool_versions']['compiler'],
                         self.compiler_version())
        source = ctx.work_dir / 'repo-source'
        self.assertFalse((ctx.work_dir / 'repo').exists())
        self.assertEqual(len(self.git(source, 'worktree', 'list', '--porcelain').split('worktree ')), 2)

    def test_make_probe_preserves_recursive_make_selection(self):
        root = self.root / 'recursive'
        root.mkdir()
        (root / 'Makefile').write_text(
            'ifndef INSIDE\n'
            '.PHONY: $(MAKECMDGOALS) descend\n'
            '$(MAKECMDGOALS): descend\n\t@:\n'
            'descend:\n\t@$(MAKE) INSIDE=1 $(MAKECMDGOALS)\n'
            'else\n'
            'CC = cc\n'
            'endif\n'
        )
        os.environ.update(CC='/missing/environment-compiler')
        self.assertEqual(read_compiler_version(root), self.compiler_version())


if __name__ == '__main__':
    unittest.main()
