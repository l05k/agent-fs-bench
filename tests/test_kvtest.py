import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_fs_bench.benchmarks import kvtest, sqlite_tools

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
            binary, provenance = sqlite_tools.build(root, "kvtest", sources, force=False)

            metadata_path = root / "kvtest-build.json"
            self.assertTrue(metadata_path.is_file())
            stored = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(stored, provenance)
            self.assertEqual(
                provenance["source_sha256"], sqlite_tools.source_hashes(sources)
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
                reused_binary, reused = sqlite_tools.build(root, "kvtest", sources, force=False)

            self.assertEqual(reused_binary, binary)
            self.assertEqual(reused, provenance)
            self.assertEqual(binary.stat().st_mtime_ns, binary_mtime)
            completed = subprocess.run([str(reused_binary)], check=False)
            self.assertEqual(completed.returncode, 0)

    def test_missing_metadata_requires_one_explicit_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, _ = sqlite_tools.build(root, "kvtest", sources, force=False)
            metadata_path = root / "kvtest-build.json"
            metadata_path.unlink()

            with mock.patch.dict(
                os.environ,
                {"CC": str(root / "missing-cc"), "PATH": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(sqlite_tools.BenchmarkError,
                                            "force_download"):
                    sqlite_tools.build(root, "kvtest", sources, force=False)

            self.assertTrue(binary.exists())

    def test_mismatched_binary_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, _ = sqlite_tools.build(root, "kvtest", sources, force=False)
            binary.write_bytes(binary.read_bytes() + b"replaced")

            with mock.patch.dict(
                os.environ,
                {"CC": str(root / "missing-cc"), "PATH": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    sqlite_tools.BenchmarkError, "does not match its build provenance"
                ):
                    sqlite_tools.build(root, "kvtest", sources, force=False)

    def test_incomplete_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            sqlite_tools.build(root, "kvtest", sources, force=False)
            metadata_path = root / "kvtest-build.json"
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
                    sqlite_tools.BenchmarkError, "incomplete build provenance"
                ):
                    sqlite_tools.build(root, "kvtest", sources, force=False)

    def test_changed_header_forces_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, _ = sqlite_tools.build(root, "kvtest", sources, force=False)

            sources["sqlite3_h"].write_text("#define EXIT_CODE 7\nint kv_helper(void);\n")
            # A changed header must invalidate even if its timestamp is preserved.
            stamp = binary.stat().st_mtime - 1
            os.utime(sources["sqlite3_h"], (stamp, stamp))
            rebuilt, _ = sqlite_tools.build(root, "kvtest", sources, force=False)
            completed = subprocess.run([str(rebuilt)], check=False)
            self.assertEqual(completed.returncode, 7)

    def test_changed_compile_flags_force_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            sources["sqlite3"].write_text(
                '#include "sqlite3.h"\n'
                '#ifndef RESULT_CODE\n#define RESULT_CODE 0\n#endif\n'
                'int kv_helper(void) { return RESULT_CODE; }\n'
            )
            binary, _ = sqlite_tools.build(root, "kvtest", sources, force=False)
            self.assertEqual(subprocess.run([str(binary)]).returncode, 0)
            flags = (*sqlite_tools.COMPILE_FLAGS, "-DRESULT_CODE=7")
            with mock.patch.object(sqlite_tools, "COMPILE_FLAGS", flags):
                rebuilt, _ = sqlite_tools.build(root, "kvtest", sources, force=False)
            self.assertEqual(subprocess.run([str(rebuilt)]).returncode, 7)

    def test_force_rebuild_restores_missing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            sqlite_tools.build(root, "kvtest", sources, force=False)
            metadata_path = root / "kvtest-build.json"
            metadata_path.unlink()

            binary, provenance = sqlite_tools.build(root, "kvtest", sources, force=True)

            self.assertTrue(metadata_path.is_file())
            self.assertEqual(
                provenance["binary_sha256"],
                hashlib.sha256(binary.read_bytes()).hexdigest(),
            )

    def test_failed_compile_leaves_cached_build_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = write_sources(root)
            binary, provenance = sqlite_tools.build(root, "kvtest", sources, force=False)
            metadata_path = root / "kvtest-build.json"
            metadata_before = metadata_path.read_bytes()
            binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()

            sources["kvtest"].write_text("this is not valid C\n")
            future = binary.stat().st_mtime + 10
            os.utime(sources["kvtest"], (future, future))

            with self.assertRaisesRegex(sqlite_tools.BenchmarkError,
                                        "failed to compile"):
                sqlite_tools.build(root, "kvtest", sources, force=False)

            self.assertEqual(
                hashlib.sha256(binary.read_bytes()).hexdigest(), binary_hash
            )
            self.assertEqual(metadata_path.read_bytes(), metadata_before)
            self.assertEqual(
                provenance["binary_sha256"], binary_hash
            )
            self.assertFalse((root / "kvtest.tmp").exists())

KVTEST_RUN_STDOUT = (
    "SQLite version: 3.54.1\n"
    "--count 2000 --max-id 2000 --random\n"
    "--cache-size -1000 --jmode wal\n"
    "--mmap 0\n"
    "Database page size: 4096\n"
    "Total elapsed time: 6.123\n"
    "Microseconds per BLOB write: 3061.500\n"
    "Content write rate: 12.3 MB/s\n"
)

KVTEST_STAT_STDOUT = (
    f"Number of entries:  {2000:8d}\n"
    f"Average value size: {2048:8d}\n"
    f"Minimum value size: {1024:8d}\n"
    f"Maximum value size: {3072:8d}\n"
    f"Page-size:          {4096:8d}\n"
    f"Page-count:         {1027:8d}\n"
    f"Freelist-count:     {0:8d}\n"
    "Integrity-check:    ok\n"
)

class OutputParsingTests(unittest.TestCase):
    def test_parses_official_output_and_reports_measured_fields(self):
        parsed = kvtest.parse_kvtest_output(KVTEST_RUN_STDOUT)
        self.assertEqual(
            parsed,
            {
                "sqlite_version": "3.54.1",
                "journal_mode": "wal",
                "cache_size": -1000,
                "page_size": 4096,
                "total_elapsed_s": 6.123,
                "microseconds_per_write": 3061.5,
                "write_rate_mb_s": 12.3,
            },
        )
        # --mmap and --synchronous are never read back, so they are not here.
        self.assertNotIn("mmap", parsed)
        self.assertNotIn("mmap_bytes", parsed)
        self.assertNotIn("synchronous", parsed)

    def test_parses_non_default_request_echoes_without_promoting_them(self):
        text = KVTEST_RUN_STDOUT.replace(
            "--jmode wal", "--jmode delete"
        ).replace("--mmap 0\n", "--mmap 1048576\n--nosync\n")
        parsed = kvtest.parse_kvtest_output(text)
        self.assertEqual(parsed["journal_mode"], "delete")
        self.assertNotIn("synchronous", parsed)

    def test_missing_measured_field_is_an_error(self):
        cases = {
            "SQLite version:": "sqlite_version",
            "--cache-size": "cache_size_and_journal_mode",
            "Database page size:": "page_size",
            "Total elapsed time:": "total_elapsed_s",
            "Microseconds per BLOB write:": "microseconds_per_write",
            "Content write rate:": "write_rate_mb_s",
        }
        for prefix, field in cases.items():
            with self.subTest(prefix=prefix):
                text = "".join(
                    line
                    for line in KVTEST_RUN_STDOUT.splitlines(keepends=True)
                    if not line.startswith(prefix)
                )
                with self.assertRaisesRegex(
                    sqlite_tools.BenchmarkError, "missing .*" + field
                ):
                    kvtest.parse_kvtest_output(text)

    def test_requires_integrity_line_in_stat_output(self):
        text = "".join(
            line
            for line in KVTEST_STAT_STDOUT.splitlines(keepends=True)
            if not line.startswith("Integrity-check:")
        )
        with self.assertRaisesRegex(sqlite_tools.BenchmarkError,
                                    "missing 'Integrity-check'"):
            kvtest.parse_integrity_check(text)

    def test_accepts_only_ok_integrity_results(self):
        self.assertEqual(
            kvtest.parse_integrity_check(KVTEST_STAT_STDOUT), "ok"
        )
        text = KVTEST_STAT_STDOUT.replace(
            "Integrity-check:    ok",
            "Integrity-check:    *** in database main ***\n"
            "Integrity-check:    Page 2 is never used",
        )
        with self.assertRaisesRegex(
            sqlite_tools.BenchmarkError, "integrity-check failed"
        ):
            kvtest.parse_integrity_check(text)

class ConfigurationTests(unittest.TestCase):
    def test_run_argv_adds_only_the_requested_mode_flags(self):
        binary, database = Path("kvtest"), Path("kvtest.sqlite")
        args = kvtest.parse_args([])
        argv = kvtest.run_argv(binary, database, args)
        self.assertIn("--multitrans", argv)
        self.assertNotIn("--nosync", argv)
        args.multitrans = False
        args.nosync = True
        argv = kvtest.run_argv(binary, database, args)
        self.assertNotIn("--multitrans", argv)
        self.assertIn("--nosync", argv)

if __name__ == "__main__":
    unittest.main()
