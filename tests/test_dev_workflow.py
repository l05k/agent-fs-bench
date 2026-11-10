import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import dev_workflow, mini_dev_workflow
from agent_fs_bench.benchmarks.build_settings import read_compiler_version
from agent_fs_bench.results import PROJECT_ROOT

BUILD_VARIABLES = (
    'CC', 'CXX', 'CPP', 'AR', 'LD', 'AS', 'NM', 'STRIP', 'OBJCOPY',
    'CFLAGS', 'CPPFLAGS', 'LDFLAGS', 'LDLIBS', 'HOSTCC', 'HOSTCXX',
    'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES', 'MAKEFILES', 'MAKELEVEL',
    'ARCH', 'SRCARCH', 'CROSS_COMPILE', 'LLVM', 'LLVM_IAS',
    'KCFLAGS', 'KAFLAGS', 'KCONFIG_CONFIG', 'KBUILD_OUTPUT',
    'BUILD_OBSERVATIONS',
)

class KernelConfigFixtureTests(unittest.TestCase):
    def test_shipped_config_selects_64_bit_x86_with_overlayfs(self):
        # tinyconfig is an allnoconfig variant: every visible boolean defaults to
        # n, which selects CONFIG_X86_32=y. CONFIG_64BIT must therefore be
        # written explicitly, and the fixture must not carry the 32-bit choice.
        path = PROJECT_ROOT / 'data' / 'dev-workflow' / dev_workflow.DEFAULT_CONFIG
        lines = {line.strip() for line in path.read_text(encoding='utf-8').splitlines()}
        self.assertIn('CONFIG_64BIT=y', lines)
        self.assertIn('CONFIG_X86_64=y', lines)
        self.assertIn('CONFIG_OVERLAY_FS=y', lines)
        self.assertNotIn('# CONFIG_64BIT is not set', lines)
        self.assertNotIn('CONFIG_X86_32=y', lines)

class ConfigurationTests(unittest.TestCase):
    def test_mini_rejects_jobs_above_the_fixed_parallelism_instead_of_clamping(self):
        with mock.patch.dict(mini_dev_workflow.CONFIG, {'jobs': 9}), \
                self.assertRaisesRegex(AssertionError, 'jobs'):
            mini_dev_workflow.parse_args([])

class FixtureResolutionTests(unittest.TestCase):
    def test_relative_fixture_dir_resolves_in_checkout(self):
        expected = PROJECT_ROOT / 'data' / 'dev-workflow'
        self.assertEqual(dev_workflow.resolve_fixture_dir('data/dev-workflow'), expected)

    def test_absolute_fixture_dir_is_taken_as_given(self):
        path = Path('/var/tmp/fixtures')
        self.assertEqual(dev_workflow.resolve_fixture_dir(str(path)), path)

    def test_linux_url_passes_through(self):
        for url in ('https://github.com/torvalds/linux.git',
                    'ssh://example.invalid/linux.git',
                    'git@example.invalid:linux.git',
                    'example.invalid:linux.git',
                    # Git reads a colon before the first slash as scp-like syntax, so a
                    # local mirror with a colon in its name needs an explicit "./".
                    'mirror:linux.git'):
            with self.subTest(url=url):
                self.assertEqual(dev_workflow.resolve_linux_url(url), url)

    def test_explicitly_local_path_with_a_colon_resolves(self):
        self.assertEqual(dev_workflow.resolve_linux_url('./mirror:linux.git'),
                         str(PROJECT_ROOT / 'mirror:linux.git'))

    def test_relative_linux_mirror_resolves_in_checkout(self):
        for name in ('data', 'mirror/linux.git'):
            with self.subTest(name=name):
                self.assertEqual(dev_workflow.resolve_linux_url(name),
                                 str(PROJECT_ROOT / name))

    def test_absolute_linux_mirror_is_taken_as_given(self):
        self.assertEqual(dev_workflow.resolve_linux_url('/var/tmp/linux.git'),
                         '/var/tmp/linux.git')

    def test_fixture_names_cover_series_files(self):
        series = {'commits': [{
            'id': 'abc', 'search_commands': 'search.cmds',
            'read_commands': 'read.cmds', 'edit_commands': 'edit.cmds',
        }]}
        self.assertEqual(dev_workflow.fixture_names(series), {
            dev_workflow.DEFAULT_SERIES, dev_workflow.DEFAULT_CONFIG,
            'search.cmds', 'read.cmds', 'edit.cmds',
        })
        self.assertEqual(mini_dev_workflow.fixture_names(series), {
            mini_dev_workflow.DEFAULT_SERIES, mini_dev_workflow.DEFAULT_BUNDLE,
            'search.cmds', 'read.cmds', 'edit.cmds',
        })

    def test_shipped_fixtures_validate(self):
        for module in (dev_workflow, mini_dev_workflow):
            fixture_dir = module.resolve_fixture_dir(module.CONFIG['fixture_dir'])
            series = module.validate_fixtures(fixture_dir)
            self.assertTrue(series['commits'])
            for name in module.fixture_names(series):
                self.assertTrue((fixture_dir / name).is_file(), name)

class MakeProbeTests(unittest.TestCase):
    def setUp(self):
        if not all(shutil.which(name) for name in ('make', 'cc')):
            self.skipTest('GNU make and a C compiler are required')
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

    def compiler_version(self, command='cc'):
        output = subprocess.run([command, '--version'], capture_output=True, text=True,
                                check=True).stdout
        return output.strip().splitlines()[0]

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
