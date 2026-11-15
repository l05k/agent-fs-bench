import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from agent_fs_bench.benchmarks import sqlite_tools
from agent_fs_bench.results import PROJECT_ROOT

class BuildDirectoryTests(unittest.TestCase):
    def test_default_is_this_checkout(self):
        self.assertEqual(
            sqlite_tools.build_directory(None, "kvtest"),
            PROJECT_ROOT / "build" / "kvtest",
        )

    def test_explicit_path_is_expanded_and_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                sqlite_tools.build_directory(root / "cache", "sqlite-speedtest"),
                (root / "cache").resolve(),
            )


class DownloadIntegrityTests(unittest.TestCase):
    def test_http_200_wrong_content_never_replaces_cached_source(self):
        expected_source = b"int main(void) { return 0; }\n"
        expected = hashlib.sha256(expected_source).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "kvtest.c"
            destination.write_bytes(expected_source)
            with mock.patch.object(sqlite_tools.urllib.request, "urlopen",
                                   return_value=io.BytesIO(b"<html>unknown revision</html>")):
                with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "SHA-256 mismatch"):
                    sqlite_tools.download("https://example.invalid/kvtest.c", destination, expected)
            self.assertEqual(destination.read_bytes(), expected_source)
            self.assertFalse(destination.with_suffix(".c.part").exists())

    def test_verified_content_is_installed(self):
        source = b"int main(void) { return 7; }\n"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "speedtest1.c"
            destination.write_bytes(b"old source")
            with mock.patch.object(sqlite_tools.urllib.request, "urlopen",
                                   return_value=io.BytesIO(source)):
                sqlite_tools.download("https://example.invalid/speedtest1.c", destination,
                                      hashlib.sha256(source).hexdigest())
            self.assertEqual(destination.read_bytes(), source)
            self.assertFalse(destination.with_suffix(".c.part").exists())

    def test_corrupt_cache_is_rejected_and_explicit_refresh_restores_it(self):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("sqlite-amalgamation-3540100/sqlite3.c", "release amalgamation")
            archive.writestr("sqlite-amalgamation-3540100/sqlite3.h", "release header")
        payloads = {
            sqlite_tools.SQLITE_AMALGAMATION: archive_buffer.getvalue(),
            "kvtest.c": b"release kvtest\n",
        }
        expected = {name: hashlib.sha256(content).hexdigest()
                    for name, content in payloads.items()}
        for corrupt_name in payloads:
            with self.subTest(corrupt_name=corrupt_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                patch = root / "kvtest-verify.patch"
                patch.write_text(
                    "--- a/kvtest.c\n+++ b/kvtest.c\n"
                    "@@ -1 +1 @@\n-release kvtest\n+release kvtest checked\n",
                    encoding="utf-8",
                )
                for name, content in payloads.items():
                    (root / name).write_bytes(content)
                (root / corrupt_name).write_bytes(b"<html>not a source file</html>")
                with mock.patch.dict(sqlite_tools.DOWNLOAD_SHA256, expected), \
                        mock.patch.object(sqlite_tools, "KVTEST_PATCH", patch), \
                        mock.patch.object(sqlite_tools.urllib.request, "urlopen") as fetch:
                    with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "SHA-256 mismatch"):
                        sqlite_tools.ensure_sources(root, "kvtest", force=False)
                    fetch.assert_not_called()
                    fetch.side_effect = [io.BytesIO(content) for content in payloads.values()]
                    sources = sqlite_tools.ensure_sources(root, "kvtest", force=True)
                self.assertEqual(sources["kvtest"].read_text(), "release kvtest checked\n")
                self.assertEqual(sources["kvtest_upstream"].read_bytes(), payloads["kvtest.c"])
                self.assertEqual(sources["kvtest_patch"], patch)
                self.assertEqual(sources["sqlite3"].read_text(), "release amalgamation")
                self.assertEqual(sources["sqlite3_h"].read_text(), "release header")

    def test_missing_checked_patch_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "kvtest.c"
            source.write_text("official\n", encoding="utf-8")
            with mock.patch.object(sqlite_tools, "KVTEST_PATCH", root / "missing.patch"):
                with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "checked patch"):
                    sqlite_tools.checked_kvtest_source(root, source)


class PatchApplicationTests(unittest.TestCase):
    def test_applies_ordered_hunks_to_exact_context(self):
        original = "one\ntwo\nthree\nfour\n"
        patch = (
            "--- a/file\n+++ b/file\n"
            "@@ -1,2 +1,2 @@\n"
            " one\n"
            "-two\n"
            "+TWO\n"
            "@@ -4 +4 @@\n"
            "-four\n"
            "+FOUR\n"
        )
        self.assertEqual(
            sqlite_tools.apply_patch(original, patch),
            "one\nTWO\nthree\nFOUR\n",
        )

    def test_rejects_mismatched_context(self):
        with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "context"):
            sqlite_tools.apply_patch(
                "one\nTWO\n",
                "--- a/file\n+++ b/file\n@@ -2 +2 @@\n-two\n+three\n",
            )

    def test_rejects_empty_patch(self):
        with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "no hunks"):
            sqlite_tools.apply_patch("one\n", "--- a/file\n+++ b/file\n")

    def test_rejects_hunk_count_mismatch(self):
        with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "line counts"):
            sqlite_tools.apply_patch(
                "one\n", "--- a/file\n+++ b/file\n@@ -1,2 +1 @@\n-one\n+ONE\n"
            )


if __name__ == "__main__":
    unittest.main()
