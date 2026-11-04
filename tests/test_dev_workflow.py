import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_fs_bench.benchmarks import dev_workflow


class ToolchainResolutionTests(unittest.TestCase):
    def test_compiler_precedence_and_llvm_variants(self):
        cases = [
            ({}, ('gcc',), None),
            ({'CROSS_COMPILE': 'cross-'}, ('cross-gcc',), None),
            ({'LLVM': '', 'CROSS_COMPILE': 'cross-'}, ('cross-gcc',), None),
            ({'LLVM': '1', 'CROSS_COMPILE': 'cross-'}, ('clang',), '1'),
            ({'LLVM': '0'}, ('clang',), '0'),
            ({'LLVM': '-17'}, ('clang-17',), '-17'),
            ({'LLVM': '/opt/llvm/bin/'}, ('/opt/llvm/bin/clang',), '/opt/llvm/bin/'),
            ({'CC': 'ccache clang -target x86_64-linux-gnu', 'LLVM': '-17',
              'CROSS_COMPILE': 'cross-'},
             ('ccache', 'clang', '-target', 'x86_64-linux-gnu'), '-17'),
        ]
        for environment, compiler, llvm in cases:
            with self.subTest(environment=environment):
                resolved = dev_workflow.resolve_toolchain(environment)
                self.assertEqual(resolved.compiler, compiler)
                self.assertEqual(resolved.llvm, llvm)

    def test_invalid_explicit_cc_is_rejected(self):
        for value in ('clang "unterminated', "''"):
            with self.subTest(cc=value), self.assertRaises(dev_workflow.WorkflowError):
                dev_workflow.resolve_toolchain({'CC': value})

    def test_preflight_requires_selected_tools_not_unrelated_gcc(self):
        for environment in ({'LLVM': '1'}, {'CROSS_COMPILE': 'cross-'}):
            selected = dev_workflow.resolve_toolchain(environment)
            available = {*dev_workflow.CORE_COMMANDS, *dev_workflow.KERNEL_COMMANDS,
                         selected.compiler[0], *selected.binutils}
            with self.subTest(environment=environment), \
                    mock.patch.object(dev_workflow, 'KERNEL_HEADERS', ()), \
                    mock.patch.object(dev_workflow.shutil, 'which',
                                      side_effect=lambda name: name if name in available else None):
                self.assertEqual(dev_workflow.missing_dependencies(selected), [])
                available.remove(selected.compiler[0])
                self.assertIn('compiler ' + selected.compiler[0],
                              dev_workflow.missing_dependencies(selected))


class ActualCompilerTests(unittest.TestCase):
    def test_make_compiles_with_explicit_cc_instead_of_makefile_default(self):
        make = shutil.which('make')
        compiler = shutil.which('cc')
        if not make or not compiler:
            self.skipTest('GNU make and a C compiler are required')
        version = subprocess.run([make, '--version'], capture_output=True, text=True, check=True)
        if 'GNU Make' not in version.stdout:
            self.skipTest('GNU make is required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Makefile').write_text(
                'CC = unavailable-default-compiler\n'
                'all:\n\t$(CC) probe.c -o probe\n'
            )
            (root / 'probe.c').write_text(
                '#include <stdio.h>\n'
                'int main(void) { puts(LABEL); return 0; }\n'
            )
            # Real compiler path containing spaces plus a real env wrapper:
            # quoted arguments must survive both make and its recipe shell.
            compiler_directory = root / 'compiler with spaces'
            compiler_directory.mkdir()
            compiler_link = compiler_directory / Path(compiler).name
            compiler_link.symlink_to(compiler)
            cc = shlex.join(['env', 'BENCHMARK_PROBE=1', str(compiler_link), '-DLABEL="a b"'])
            selected = dev_workflow.resolve_toolchain({'CC': cc})
            environment = {key: value for key, value in os.environ.items()
                           if key not in ('CC', 'LLVM', 'CROSS_COMPILE', 'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES')}
            environment['CC'] = cc
            ignored = subprocess.run([make, '-s'], cwd=root, env=environment,
                                     capture_output=True, text=True)
            self.assertNotEqual(ignored.returncode, 0)
            subprocess.run(dev_workflow.make_command(selected, '-s'), cwd=root,
                           env=environment, capture_output=True, text=True, check=True)
            observed = subprocess.run([str(root / 'probe')], capture_output=True,
                                      text=True, check=True)
            self.assertEqual(observed.stdout, 'a b\n')


if __name__ == '__main__':
    unittest.main()
