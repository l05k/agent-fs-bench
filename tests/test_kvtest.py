import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_fs_bench.benchmarks import kvtest
from agent_fs_bench.results import RunContext
from agent_fs_bench.targets import Target



def available_compiler():
    requested = os.environ.get("CC")
    if requested:
        return shutil.which(requested)
    return shutil.which("cc") or shutil.which("gcc")


def write_sources(root):
    sources = {
        "kvtest": root / "kvtest.c",
        "sqlite3": root / "sqlite3.c",
        "sqlite3_h": root / "sqlite3.h",
    }
    sources["sqlite3_h"].write_text("#define EXIT_CODE 0\nint kv_helper(void);\n")
    sources["sqlite3"].write_text(
        '#include "sqlite3.h"\nint kv_helper(void) { return EXIT_CODE; }\n'
    )
    sources["kvtest"].write_text(
        '#include "sqlite3.h"\n'
        "int main(void) { return kv_helper(); }\n"
    )
    return sources


@unittest.skipUnless(available_compiler(), "C compiler not available")
class BuildProvenanceTests(unittest.TestCase):
    def test_caches_and_reuses_without_current_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, provenance = kvtest.build(root, sources, force=False)

            metadata_path = root / kvtest.BUILD_METADATA_FILENAME
            self.assertTrue(metadata_path.is_file())
            stored = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(stored, provenance)
            self.assertEqual(
                provenance["source_sha256"], kvtest.source_hashes(sources)
            )
            self.assertEqual(
                provenance["binary_sha256"],
                hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
            self.assertEqual(provenance["compiler_argv"][0],
                             provenance["compiler"])
            self.assertTrue(provenance["compiler_version"])
            binary_mtime = binary.stat().st_mtime_ns

            with mock.patch.dict(
                os.environ,
                {"CC": str(root / "missing-cc"), "PATH": ""},
                clear=False,
            ):
                reused_binary, reused = kvtest.build(
                    root, sources, force=False
                )

            self.assertEqual(reused_binary, binary)
            self.assertEqual(reused, provenance)
            self.assertEqual(binary.stat().st_mtime_ns, binary_mtime)
            completed = subprocess.run([str(reused_binary)], check=False)
            self.assertEqual(completed.returncode, 0)

    def test_missing_metadata_requires_one_explicit_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, _ = kvtest.build(root, sources, force=False)
            metadata_path = root / kvtest.BUILD_METADATA_FILENAME
            metadata_path.unlink()

            with mock.patch.dict(
                os.environ,
                {"CC": str(root / "missing-cc"), "PATH": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(kvtest.BenchmarkError,
                                            "force_download"):
                    kvtest.build(root, sources, force=False)

            self.assertTrue(binary.exists())

    def test_mismatched_binary_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, _ = kvtest.build(root, sources, force=False)
            binary.write_bytes(binary.read_bytes() + b"replaced")

            with mock.patch.dict(
                os.environ,
                {"CC": str(root / "missing-cc"), "PATH": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    kvtest.BenchmarkError, "does not match its build provenance"
                ):
                    kvtest.build(root, sources, force=False)

    def test_incomplete_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            kvtest.build(root, sources, force=False)
            metadata_path = root / kvtest.BUILD_METADATA_FILENAME
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            del metadata["compiler_version"]
            metadata_path.write_text(
                json.dumps(metadata), encoding="utf-8"
            )

            with mock.patch.dict(
                os.environ,
                {"CC": str(root / "missing-cc"), "PATH": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    kvtest.BenchmarkError, "incomplete build provenance"
                ):
                    kvtest.build(root, sources, force=False)

    def test_changed_header_forces_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, _ = kvtest.build(root, sources, force=False)

            sources["sqlite3_h"].write_text("#define EXIT_CODE 7\nint kv_helper(void);\n")
            # A changed header must invalidate even if its timestamp is preserved.
            stamp = binary.stat().st_mtime - 1
            os.utime(sources["sqlite3_h"], (stamp, stamp))
            rebuilt, _ = kvtest.build(root, sources, force=False)
            completed = subprocess.run([str(rebuilt)], check=False)
            self.assertEqual(completed.returncode, 7)

    def test_force_rebuild_restores_missing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            kvtest.build(root, sources, force=False)
            metadata_path = root / kvtest.BUILD_METADATA_FILENAME
            metadata_path.unlink()

            binary, provenance = kvtest.build(root, sources, force=True)

            self.assertTrue(metadata_path.is_file())
            self.assertEqual(
                provenance["binary_sha256"],
                hashlib.sha256(binary.read_bytes()).hexdigest(),
            )

    def test_failed_compile_leaves_cached_build_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, provenance = kvtest.build(root, sources, force=False)
            metadata_path = root / kvtest.BUILD_METADATA_FILENAME
            metadata_before = metadata_path.read_bytes()
            binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()

            sources["kvtest"].write_text("this is not valid C\n")
            future = binary.stat().st_mtime + 10
            os.utime(sources["kvtest"], (future, future))

            with self.assertRaisesRegex(kvtest.BenchmarkError,
                                        "failed to compile"):
                kvtest.build(root, sources, force=False)

            self.assertEqual(
                hashlib.sha256(binary.read_bytes()).hexdigest(), binary_hash
            )
            self.assertEqual(metadata_path.read_bytes(), metadata_before)
            self.assertEqual(
                provenance["binary_sha256"], binary_hash
            )
            self.assertFalse((root / "kvtest.tmp").exists())


class JmodeValidationTests(unittest.TestCase):
    def test_all_allowed_modes_normalize_from_config(self):
        for mode in kvtest.JMODE_CHOICES:
            for configured in (mode, mode.upper(), mode.capitalize()):
                with self.subTest(configured=configured), \
                        mock.patch.dict(kvtest.CONFIG, {"jmode": configured}):
                    self.assertEqual(kvtest.parse_args([]).jmode, mode)

    def test_invalid_mode_is_rejected_by_parse_args(self):
        for configured in ("wla", "WLA"):
            with self.subTest(configured=configured), \
                    mock.patch.dict(kvtest.CONFIG, {"jmode": configured}):
                with self.assertRaisesRegex(kvtest.BenchmarkError,
                                            "invalid jmode"):
                    kvtest.parse_args([])

    def test_run_does_not_emit_for_invalid_mode(self):
        output = io.StringIO()
        context = RunContext("kvtest", "invalid", Target("fs", Path("/mnt/fs")),
                             Path("/missing/work"), Path("/missing/results"), {}, {}, output)
        args = SimpleNamespace(jmode="wla", build_dir=None)
        with self.assertRaises(kvtest.BenchmarkError):
            kvtest.run(context, args)
        self.assertEqual(output.getvalue(), "")

    def test_direct_measure_rejects_invalid_mode_before_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                jmode="wla",
                build_dir=root / "build",
                force_download=False,
                target_dir=root / "target",
            )
            with mock.patch.object(kvtest, "ensure_sources") as ensure_sources:
                with self.assertRaisesRegex(kvtest.BenchmarkError,
                                            "invalid jmode"):
                    kvtest.measure(args)
            ensure_sources.assert_not_called()
            self.assertFalse((root / "build").exists())
            self.assertFalse((root / "target").exists())




if __name__ == "__main__":
    unittest.main()
