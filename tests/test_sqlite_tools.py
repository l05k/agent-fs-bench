import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from agent_fs_bench.benchmarks import sqlite_tools


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
            "kvtest.c": b"release kvtest",
        }
        expected = {name: hashlib.sha256(content).hexdigest()
                    for name, content in payloads.items()}
        for corrupt_name in payloads:
            with self.subTest(corrupt_name=corrupt_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, content in payloads.items():
                    (root / name).write_bytes(content)
                (root / corrupt_name).write_bytes(b"<html>not a source file</html>")
                with mock.patch.dict(sqlite_tools.DOWNLOAD_SHA256, expected), \
                        mock.patch.object(sqlite_tools.urllib.request, "urlopen") as fetch:
                    with self.assertRaisesRegex(sqlite_tools.BenchmarkError, "SHA-256 mismatch"):
                        sqlite_tools.ensure_sources(root, "kvtest", force=False)
                    fetch.assert_not_called()
                    fetch.side_effect = [io.BytesIO(content) for content in payloads.values()]
                    sources = sqlite_tools.ensure_sources(root, "kvtest", force=True)
                self.assertEqual(sources["kvtest"].read_bytes(), payloads["kvtest.c"])
                self.assertEqual(sources["sqlite3"].read_text(), "release amalgamation")
                self.assertEqual(sources["sqlite3_h"].read_text(), "release header")


if __name__ == "__main__":
    unittest.main()
