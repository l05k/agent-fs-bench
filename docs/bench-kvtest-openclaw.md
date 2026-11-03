# OpenClaw-shaped SQLite writes via official `kvtest`

Runs SQLite's own [`test/kvtest.c`](https://github.com/sqlite/sqlite/blob/version-3.54.1/test/kvtest.c)
with `--multitrans` so every BLOB overwrite commits in its own transaction.
That commit shape is the closest match among SQLite's shipped test programs to
OpenClaw's per-turn transcript write.

用 SQLite 官方 `kvtest --multitrans` 做「每次写一个事务」的基准，用来近似
OpenClaw 每个 turn 一次提交的写路径。

## Why not `speedtest1`

`speedtest1` batches roughly 750 statements per `COMMIT` (`--size 10` produced
21023 INSERTs across 28 commits), so it measures B-tree throughput rather than
commit cost. Measured on one machine, `speedtest1 --size 100` moved only 16%
between `journal_mode=DELETE`, `WAL`, and `WAL --nosync`, which shows the
journal path is barely exercised. `kvtest --multitrans` commits per write and
does react to the journal: 4096-byte writes cost 12.5 µs/op batched versus
38.75 µs/op with `--multitrans`.

## Run / 运行

```sh
uv run kvtest_openclaw_fs.py --dir /mnt/testfs
uv run kvtest_openclaw_fs.py --dir /mnt/testfs --count 5000 --size 4096 --variance 2048
uv run kvtest_openclaw_fs.py --dir /mnt/testfs --jmode delete          # journal ablation
uv run kvtest_openclaw_fs.py --dir /mnt/testfs --nosync                # durability ablation
uv run kvtest_openclaw_fs.py --dir /mnt/testfs --repeats 3             # repeat the write phase
```

Defaults: `--count 2000`, `--size 2048`, `--variance 1024`,
`--cache-size 1000` (KiB), `--jmode wal`, `--repeats 1`.

The first run downloads the SQLite 3.54.1 amalgamation and `kvtest.c` into
`build/` next to the script (git-ignored) and compiles `build/kvtest` with
`cc -O2`. Later runs reuse both. Set `CC` to pick a compiler,
`--build-dir` to move the download/compile cache, or `--force-download` to
refresh the sources.

## Single-file deployment / 单文件拷贝

Like the other benchmark scripts this one is self-contained: the shared
`results`-directory helper is inlined between generated markers, and no module
outside the Python standard library is imported. Copy just
`kvtest_openclaw_fs.py` to the test machine and run it with the system
`python3` (verified on 3.9.6); it needs no checkout, no `uv`, and no
`docs/` or `data/` directory.

What it does need, on the test machine only:

- network access on the first run, to fetch the amalgamation and `kvtest.c`
  from `sqlite.org` and `raw.githubusercontent.com`;
- a C compiler (`cc` or `gcc`, or whatever `CC` points at);
- SQLite's `fts5` is not required, because `kvtest` uses a plain table.

Both the results directory and the build cache are resolved relative to the
script's own location, so a copied script writes
`<copy-dir>/results/kvtest_openclaw_fs/<UTC-run-id>/` and
`<copy-dir>/build/`, never into the original checkout.

`--count` is both the number of rows seeded by `kvtest init` and the number of
writes in the timed phase, because `kvtest run` touches `--count` keys.

## What the numbers mean

`kvtest` prints `Microseconds per BLOB write` and `Content write rate`; the
harness parses both and repeats them per `--repeats` run. Each run overwrites
existing rows, so the first repeat writes into a fresh table and later repeats
write into a populated one — that difference is real and is kept in the report
rather than averaged away.

`result.json` also records the effective pragmas, the SQLite versions, the
exact argv of every phase, and the raw `kvtest` stdout.

## Pragma matching against OpenClaw

Reference: `src/infra/sqlite-wal.ts` and `src/state/openclaw-agent-db.ts` at
`openclaw@5f6f15fed0d6ba618cf781c9686a8b93f499564b`.

| Parameter | OpenClaw agent DB | Reached here | Note |
| --- | --- | --- | --- |
| `journal_mode` | `WAL` | `WAL` | `--jmode wal` |
| `synchronous` | `NORMAL` | **`FULL`** | kvtest offers only default `FULL` or `--nosync` (`OFF`) |
| `busy_timeout` | `5000` ms | not set | no kvtest option |
| `foreign_keys` | `ON` | n/a | single table, no foreign keys |
| `journal_size_limit` | `64 MiB` | not set | no kvtest option |
| `auto_vacuum` | `INCREMENTAL` on fresh db | not set | `kvtest init` cannot set it |
| `wal_autocheckpoint` | `0` (worker-maintained) | `1000` | SQLite default; see below |
| `cache_size` | default | `--cache-size`, default 1000 KiB | |
| `mmap_size` | default | `--mmap`, default 0 | |

`wal_autocheckpoint` is the gap that matters most. OpenClaw sets it to `0`
because a worker checkpoints on a 10 s tick; this harness has no worker.
`kvtest` can only force it to `0` via `--nocheckpoint`, which lets the WAL grow
without bound and pushes one large checkpoint to the end of the run — measured
at 1.98 GiB of WAL after 6 s on one machine, which is a state OpenClaw never
occupies. The default of 1000 pages (about 4 MiB at a 4 KiB page size) keeps
the WAL bounded, so the harness leaves it alone and records the deviation.

`synchronous=FULL` is *stricter* than OpenClaw's `NORMAL`, so these numbers are
an upper bound on durability work, not a faithful reproduction.

Deliberate workload gaps:

- **No secondary indexes.** OpenClaw maintains 12 indexes across 7 tables in
  the write path; `kv` has one primary key.
- **No FTS5.** OpenClaw writes an FTS5 projection inside the same transaction.
  Measured in `openclaw_session_bench.py`, disabling it moved 383 → 646
  turns/s, so FTS is 30–40% of that workload's cost. `kvtest` writes none.
- **Uniform payloads.** OpenClaw payloads are 90% 1–4 KiB, 9% 256 KiB, 1% 1 MiB
  JSON text; `kvtest` writes fixed-size BLOBs varied only by `--variance`.

`kvtest` therefore measures the **commit + BLOB overwrite lower bound** of the
OpenClaw write path, not the whole path.

## Artifacts

```text
<--dir>/kv-openclaw.sqlite   database + WAL/SHM on the filesystem under test
build/                       downloaded sources and the compiled kvtest (git-ignored)
results/kvtest_openclaw_fs/<UTC-run-id>/
  result.json                parsed timings, pragmas, versions, per-repeat argv
  summary.txt                one-screen human summary
  manifest.txt               provenance and effective settings
  kvtest-stdout.txt          raw kvtest output for every phase
```

A run started within the same second reuses and overwrites that second's
directory.
