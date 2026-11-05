import unittest

from agent_fs_bench.benchmarks import sqlite_speedtest, sqlite_tools


# Official SQLite 3.54.1 main --size 1 output from a Linux/ext4 smoke run.
SPEEDTEST_STDOUT = """-- Speedtest1 for SQLite 3.54.1 2026-10-16 14:45:20 555e07ef
       Begin testset "main"
 100 - 500 INSERTs into table with no index........................    0.001s
 110 - 500 ordered INSERTS with one index/PK.......................    0.001s
 120 - 500 unordered INSERTS with one index/PK.....................    0.001s
 130 - 25 SELECTS, numeric BETWEEN, unindexed......................    0.001s
 140 - 10 SELECTS, LIKE, unindexed.................................    0.000s
 142 - 10 SELECTS w/ORDER BY, unindexed............................    0.000s
 145 - 10 SELECTS w/ORDER BY and LIMIT, unindexed..................    0.001s
 150 - CREATE INDEX five times.....................................    0.001s
 160 - 100 SELECTS, numeric BETWEEN, indexed.......................    0.000s
 161 - 100 SELECTS, numeric BETWEEN, PK............................    0.001s
 170 - 100 SELECTS, text BETWEEN, indexed..........................    0.000s
 180 - 500 INSERTS with three indexes..............................    0.001s
 190 - DELETE and REFILL one table.................................    0.002s
 200 - VACUUM......................................................    0.002s
 210 - ALTER TABLE ADD COLUMN, and query...........................    0.001s
 230 - 100 UPDATES, numeric BETWEEN, indexed.......................    0.001s
 240 - 500 UPDATES of individual rows..............................    0.001s
 250 - One big UPDATE of the whole 500-row table...................    0.001s
 260 - Query added column after filling............................    0.000s
 270 - 100 DELETEs, numeric BETWEEN, indexed.......................    0.001s
 280 - 500 DELETEs of individual rows..............................    0.001s
 290 - Refill two 500-row tables using REPLACE.....................    0.002s
 300 - Refill a 500-row table using (b&1)==(a&1)...................    0.003s
 310 - 100 four-ways joins.........................................    0.000s
 320 - subquery in result set......................................    0.000s
 400 - 700 REPLACE ops on an IPK...................................    0.001s
 410 - 700 SELECTS on an IPK.......................................    0.002s
 500 - 700 REPLACE on TEXT PK......................................    0.002s
 510 - 700 SELECTS on a TEXT PK....................................    0.002s
 520 - 700 SELECT DISTINCT.........................................    0.000s
 980 - PRAGMA integrity_check......................................    0.000s
 990 - ANALYZE.....................................................    0.001s
       TOTAL.......................................................    0.031s
"""


class SpeedtestOutputTests(unittest.TestCase):
    def test_parses_stage_seconds_separately_from_official_total(self):
        result = sqlite_speedtest.parse_speedtest_output(SPEEDTEST_STDOUT)
        self.assertEqual(result["sqlite_version"], "3.54.1")
        self.assertEqual(result["official_total_s"], 0.031)
        self.assertEqual(result["stages"][0], {
            "id": 100,
            "description": "500 INSERTs into table with no index",
            "elapsed_s": 0.001,
        })
        lookup = next(stage for stage in result["stages"] if stage["id"] == 510)
        self.assertEqual(lookup["elapsed_s"], 0.002)

    def test_rejects_missing_stage_despite_complete_total(self):
        text = "\n".join(line for line in SPEEDTEST_STDOUT.splitlines()
                         if not line.lstrip().startswith("240 -"))
        with self.assertRaises(sqlite_tools.BenchmarkError):
            sqlite_speedtest.parse_speedtest_output(text)

    def test_rejects_total_inconsistent_with_stage_timings(self):
        text = SPEEDTEST_STDOUT.replace("0.031s", "3.100s")
        with self.assertRaises(sqlite_tools.BenchmarkError):
            sqlite_speedtest.parse_speedtest_output(text)

    def test_rejects_missing_total_or_version(self):
        for prefix in ("-- Speedtest1", "       TOTAL"):
            text = "\n".join(line for line in SPEEDTEST_STDOUT.splitlines()
                             if not line.startswith(prefix))
            with self.subTest(prefix=prefix), self.assertRaises(sqlite_tools.BenchmarkError):
                sqlite_speedtest.parse_speedtest_output(text)


if __name__ == "__main__":
    unittest.main()
