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
uv run python -m agent_fs_bench run kvtest --dirs /mnt/fs-a
```

There are no workload flags. Payload size/variance, writes, cache size, journal
mode, mmap, repeats, build directory and forced refresh are the deliberate
`CONFIG` mapping at the top of `agent_fs_bench/benchmarks/kvtest.py`. The
experiment inputs — size, variance, count, cache size, journal mode, mmap and
repeats — are recorded in `parameters` as requests; `build_dir` and
`force_download` are run management and are not recorded. Change that code for
an ablation; do not commit private settings. Defaults: `count=2000`,
`size=2048`, `variance=1024`, `cache_size=1000` KiB, `jmode=wal`, `repeats=1`,
`mmap=0`, `nosync=false` (synchronous FULL).

The first run downloads the SQLite 3.54.1 amalgamation and `kvtest.c` into
`build/kvtest/` at the repository root (git-ignored) and compiles `kvtest` with
`-O2` using the compiler named by `CC`, or `cc` then `gcc` when `CC` is unset.
The compiler path, exact compile argv, compiler `--version` line, source hashes
and binary hash are saved beside it in `build/kvtest/kvtest-build.json`.

Later runs reuse the binary only when it is newer than `kvtest.c`, `sqlite3.c`
and `sqlite3.h`, and its metadata matches both the binary hash and the current
source hashes. Reuse does not resolve or run any compiler, so a host that only
consumes an existing cache does not need one; rows record the cached compiler's
`--version` line and the source/binary hashes, not the current `CC`. If an
up-to-date binary has missing or incomplete metadata, or metadata that does not
match the binary hash, the run fails and asks for exactly one explicit rebuild
(`CONFIG["force_download"] = True`, or delete the binary and metadata); it never
guesses provenance or treats an unproven binary as current. Changing a source or
header rebuilds, as does setting force. A failed compile or version probe leaves
the previous binary and metadata untouched. Set `CC` to choose a compiler and
`CONFIG["build_dir"]` to move the download/compile cache. Paths and the exact
compile argv stay in `build/kvtest/kvtest-build.json`; they are not copied into
JSONL.

`CONFIG["jmode"]` is case-insensitive and must be one of `delete`, `truncate`,
`persist`, `memory`, `wal`, `off`; the requested value in `parameters.jmode` is
lowercase. The mode kvtest's own connection reports is in
`details.observed.journal_mode` and may differ for nonpersistent modes. An
invalid request fails the run before any download, build or database work.

## Deployment / 部署

Deploy the checkout with the shared Python package and use Python 3.12+.
Install the project's DuckDB dependency with `uv sync --locked`; the shared
runner derives `summary.csv` beside JSONL after the target loop completes. A
failure raised by a target aborts before export, so earlier JSONL rows remain
but the automatic summary CSV is not written. No single-file helper generation
or separate script deployment is supported.
The test host needs network access for the first source download and a C
compiler (`CC`, `cc` or `gcc`) for the first build; a valid cached build needs
neither. FTS5 is not required. The common runner allocates a fresh target
subdirectory for each run. The first build uses the downloaded SQLite header and
does not require a system SQLite development package.


`CONFIG["count"]` is both the number of rows seeded by `kvtest init` and the
number of writes in the timed phase, because `kvtest run` touches that many
keys.

## What the numbers mean

`kvtest` prints `SQLite version`, `--cache-size ... --jmode ...`,
`Database page size`, `Total elapsed time`, `Microseconds per BLOB write` and
`Content write rate`. The harness requires all of them; a missing field aborts
the run instead of defaulting a value. After all timed repeats finish, it
runs the same official binary's `stat <db>` once and requires
`Integrity-check: ok`.
Each timed run overwrites randomly selected existing initialized rows
(`kvtest --random`). Later repeats retain the rows left by earlier overwrites;
each repeat is a separate JSONL measurement, not an averaged result.

Each JSONL row separates requests from observations and measured timings:

- `parameters` holds the experiment inputs (`size`, `variance`, `count`,
  `cache_size_kib`, `jmode`, `nosync`, `mmap_bytes`, `repeats`), plus
  `tool_versions` (`sqlite` parsed from kvtest's own stdout, `compiler` from the
  build cache), `source_sha256` for the build inputs and `binary_sha256`.
- `details.observed` holds the values kvtest read back from SQLite:
  `journal_mode`, `cache_size` (raw PRAGMA value; negative means KiB) and
  `page_size` in bytes. `details.integrity_check` is the final official
  `stat` result.
- `metrics` holds only `microseconds_per_write`, `write_rate_mb_s` and
  `total_elapsed_s`; requested settings are not repeated there.

The SQLite version is the one compiled into the measured binary, not the
Python `sqlite3` module version. `--mmap` is only echoed by kvtest and
`synchronous` is never printed, so neither is reported as observed.

## Pragma matching against OpenClaw

Reference: `src/infra/sqlite-wal.ts` and `src/state/openclaw-agent-db.ts` at
`openclaw@5f6f15fed0d6ba618cf781c9686a8b93f499564b`.

| Parameter | OpenClaw agent DB | Requested here | Read back by kvtest |
| --- | --- | --- | --- |
| `journal_mode` | `WAL` | `CONFIG["jmode"] = "wal"` | yes, `details.observed.journal_mode` |
| `synchronous` | `NORMAL` | `FULL`, or `OFF` with `--nosync` | **no**, never printed |
| `busy_timeout` | `5000` ms | not set; no kvtest option | no |
| `foreign_keys` | `ON` | n/a (single table, no foreign keys) | no |
| `journal_size_limit` | `64 MiB` | not set; no kvtest option | no |
| `auto_vacuum` | `INCREMENTAL` on fresh db | not set; `kvtest init` cannot set it | no |
| `wal_autocheckpoint` | `0` (worker-maintained) | not set; SQLite default 1000 | **no** |
| `cache_size` | default | `CONFIG["cache_size"]`, default 1000 KiB | yes, `details.observed.cache_size` |
| `mmap_size` | default | `CONFIG["mmap"]`, default 0 | **no**, request is only echoed |
| `page_size` | default | database default | yes, `details.observed.page_size` |
| SQLite version | n/a | source pinned to 3.54.1 | yes, `parameters.tool_versions.sqlite` |

`wal_autocheckpoint` is the gap that matters most. OpenClaw sets it to `0`
because a worker checkpoints on a 10 s tick; this harness has no worker.
`kvtest` can only force it to `0` via `--nocheckpoint`, which lets the WAL grow
without bound and pushes one large checkpoint to the end of the run — measured
at 1.98 GiB of WAL after 6 s on one machine, which is a state OpenClaw never
occupies. The default of 1000 pages (about 4 MiB at a 4 KiB page size) keeps
the WAL bounded, so the harness leaves it alone and does not claim a different
observed value.

`synchronous=FULL` is *stricter* than OpenClaw's `NORMAL`, so these numbers are
an upper bound on durability work, not a faithful reproduction. It is a request
only: kvtest never prints the synchronous mode, so it is not recorded as an
observed setting.

Deliberate workload gaps:

- **No secondary indexes.** OpenClaw maintains 12 indexes across 7 tables in
  the write path; `kv` has one primary key.
- **No FTS5.** OpenClaw writes an FTS5 projection inside the same transaction;
  `kvtest` writes none.
- **Uniform payloads.** OpenClaw payloads are 90% 1–4 KiB, 9% 256 KiB, 1% 1 MiB
  JSON text; `kvtest` writes fixed-size BLOBs varied only by
  `CONFIG["variance"]`.

`kvtest` is a **single-table random BLOB overwrite comparison benchmark**, with
one commit per operation, not a strict lower bound on OpenClaw latency: the
simpler schema and stronger default synchronization pull costs in opposite
directions. `Microseconds per BLOB write` is total elapsed time divided by the
operation count, including connection setup and the final close/checkpoint;
it is not a sampled transaction latency distribution.

The harness passes a negative SQLite `cache_size` so `CONFIG["cache_size"]` is
in KiB, not pages. `details.observed.cache_size` is the raw PRAGMA value
kvtest printed, so `-1000` means 1000 KiB. `details.observed.journal_mode` comes
from kvtest's own connection output (nonpersistent modes can differ on a later
inspection connection). After all timed repeats finish, the harness runs
`kvtest stat <db>` once with the same binary, outside any timing; any
`Integrity-check` result other than `ok` is a hard error before a row is
emitted.

## Artifacts

```text
<target>/agent-fs-bench/kvtest/<run-id>/kv-openclaw.sqlite
                             fresh database + WAL/SHM on the target
build/kvtest/                sources, compiled binary (git-ignored), and
                             kvtest-build.json build provenance
results/kvtest/<run-id>/results.jsonl
                             one complete measurement per repeat
```

Source revision, compiler and workload requests must match when comparing runs;
each row records the source/binary hashes and the observed SQLite version.
`microseconds_per_write` amortizes connection setup and final close/checkpoint;
it is not a distribution of individually sampled write latencies.
