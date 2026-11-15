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
    "--cache-size -1000 --jmode wal --synchronous 2\n"
    "--mmap 0\n"
    "Completed updates: 2000 of 2000\n"
    "Committed transactions: 2000\n"
    "Database page size: 4096\n"
    "Total elapsed time: 6.123\n"
    "Microseconds per BLOB write: 3061.500\n"
    "Content write rate: 12.3 MB/s\n"
)

KVTEST_READ_STDOUT = (
    "SQLite version: 3.54.1\n"
    "--count 2000 --max-id 2000 --random\n"
    "--cache-size -1000 --jmode wal --synchronous 2\n"
    "--mmap 0\n"
    "Completed reads: 2000 of 2000\n"
    "Database page size: 4096\n"
    "Total elapsed time: 3.045\n"
    "Microseconds per BLOB read: 1522.500\n"
    "Content read rate: 25.0 MB/s\n"
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
    def test_parses_write_output_and_reports_measured_fields(self):
        parsed = kvtest.parse_kvtest_output(KVTEST_RUN_STDOUT, update=True)
        self.assertEqual(
            parsed,
            {
                "sqlite_version": "3.54.1",
                "journal_mode": "wal",
                "cache_size": -1000,
                "synchronous": 2,
                "page_size": 4096,
                "total_elapsed_s": 6.123,
                "microseconds_per_write": 3061.5,
                "write_rate_mb_s": 12.3,
                "completed_updates": 2000,
                "attempted_updates": 2000,
                "committed_transactions": 2000,
            },
        )
        # --mmap is only echoed by kvtest, so it is not reported as observed.
        self.assertNotIn("mmap", parsed)
        self.assertNotIn("mmap_bytes", parsed)

    def test_parses_separate_read_output(self):
        parsed = kvtest.parse_kvtest_output(KVTEST_READ_STDOUT, update=False)
        self.assertEqual(
            parsed,
            {
                "sqlite_version": "3.54.1",
                "journal_mode": "wal",
                "cache_size": -1000,
                "synchronous": 2,
                "page_size": 4096,
                "total_elapsed_s": 3.045,
                "microseconds_per_read": 1522.5,
                "read_rate_mb_s": 25.0,
                "completed_reads": 2000,
                "attempted_reads": 2000,
            },
        )

    def test_parses_non_default_request_echoes(self):
        text = KVTEST_RUN_STDOUT.replace(
            "--jmode wal", "--jmode delete"
        ).replace(
            "--synchronous 2", "--synchronous 0"
        ).replace("--mmap 0\n", "--mmap 1048576\n--nosync\n")
        parsed = kvtest.parse_kvtest_output(text, update=True)
        self.assertEqual(parsed["journal_mode"], "delete")
        self.assertEqual(parsed["synchronous"], 0)

    def test_missing_measured_field_is_an_error(self):
        cases = {
            "SQLite version:": "sqlite_version",
            "--cache-size": "settings",
            "Database page size:": "page_size",
            "Total elapsed time:": "total_elapsed_s",
            "Microseconds per BLOB write:": "microseconds_per_write",
            "Content write rate:": "write_rate_mb_s",
            "Completed updates:": "completed_updates",
            "Committed transactions:": "committed_transactions",
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
                    kvtest.parse_kvtest_output(text, update=True)

    def test_missing_read_field_is_an_error(self):
        with self.assertRaisesRegex(
            sqlite_tools.BenchmarkError, "missing .*completed_reads"
        ):
            kvtest.parse_kvtest_output(
                KVTEST_READ_STDOUT.replace("Completed reads: 2000 of 2000\n", ""),
                update=False,
            )

    def test_effective_settings_must_match_requested(self):
        args = kvtest.parse_args([])
        args.jmode = "wal"
        cases = {
            "journal_mode": KVTEST_RUN_STDOUT.replace(
                "--jmode wal", "--jmode truncate"),
            "cache_size": KVTEST_RUN_STDOUT.replace(
                "--cache-size -1000", "--cache-size -2000"),
            "synchronous": KVTEST_RUN_STDOUT.replace(
                "--synchronous 2", "--synchronous 1"),
        }
        for field, text in cases.items():
            with self.subTest(field=field):
                parsed = kvtest.parse_kvtest_output(text, update=True)
                with self.assertRaisesRegex(
                    sqlite_tools.BenchmarkError, field
                ):
                    kvtest.verify_output(
                        parsed, args, update=True, where="task 0 on host"
                    )

    def test_completed_and_committed_counts_are_checked(self):
        args = kvtest.parse_args([])
        args.jmode = "wal"
        parsed = kvtest.parse_kvtest_output(KVTEST_RUN_STDOUT, update=True)
        kvtest.verify_output(parsed, args, update=True, where="task 0 on host")

        cases = {
            "completed_updates": KVTEST_RUN_STDOUT.replace(
                "Completed updates: 2000 of 2000",
                "Completed updates: 1999 of 2000"),
            "attempted_updates": KVTEST_RUN_STDOUT.replace(
                "Completed updates: 2000 of 2000",
                "Completed updates: 2000 of 2001"),
            "committed_transactions": KVTEST_RUN_STDOUT.replace(
                "Committed transactions: 2000",
                "Committed transactions: 1"),
        }
        for field, text in cases.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    sqlite_tools.BenchmarkError, field
                ):
                    kvtest.verify_output(
                        kvtest.parse_kvtest_output(text, update=True),
                        args, update=True, where="task 0 on host",
                    )

    def test_batch_mode_expects_one_committed_transaction(self):
        args = kvtest.parse_args([])
        args.jmode = "wal"
        args.multitrans = False
        text = KVTEST_RUN_STDOUT.replace(
            "Committed transactions: 2000", "Committed transactions: 1")
        kvtest.verify_output(
            kvtest.parse_kvtest_output(text, update=True),
            args, update=True, where="task 0 on host",
        )

    def test_read_counts_are_checked(self):
        args = kvtest.parse_args([])
        args.jmode = "wal"
        bad = KVTEST_READ_STDOUT.replace(
            "Completed reads: 2000 of 2000", "Completed reads: 1999 of 2000")
        with self.assertRaisesRegex(
            sqlite_tools.BenchmarkError, "completed_reads"
        ):
            kvtest.verify_output(
                kvtest.parse_kvtest_output(bad, update=False),
                args, update=False, where="task 0 on host",
            )

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
    def test_run_argv_separates_write_and_read_phases(self):
        binary, database = Path("kvtest"), Path("kvtest.sqlite")
        args = kvtest.parse_args([])
        argv = kvtest.run_argv(binary, database, args, update=True)
        self.assertIn("--update", argv)
        self.assertIn("--multitrans", argv)
        self.assertNotIn("--nosync", argv)
        args.multitrans = False
        args.nosync = True
        argv = kvtest.run_argv(binary, database, args, update=True)
        self.assertNotIn("--multitrans", argv)
        self.assertIn("--nosync", argv)
        argv = kvtest.run_argv(binary, database, args, update=False)
        self.assertNotIn("--update", argv)
        self.assertNotIn("--multitrans", argv)
        self.assertIn("--random", argv)
        self.assertIn("--nosync", argv)

if __name__ == "__main__":
    unittest.main()
