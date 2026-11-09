# Official SQLite SQL microbenchmark

`run sqlite-speedtest` runs the unmodified official
[`test/speedtest1.c`](https://sqlite.org/src/raw/test/speedtest1.c?ci=version-3.54.1)
with `--testset main`. SQLite 3.54.1 and its amalgamation are pinned. This is a
single-connection SQL workload on database files in the target directory, not an
Agent replay. It complements `kvtest`'s controlled BLOB transaction experiment.

## Run and configuration

```sh
uv run python -m agent_fs_bench run sqlite-speedtest --dirs /mnt/fs-a /mnt/fs-b
```

Mount targets externally. There are no workload flags; change `CONFIG` in
`agent_fs_bench/benchmarks/sqlite_speedtest.py` for an experiment. Defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `size` | `100` | Official relative scale, not database bytes |
| `cache_size` | `2000` | KiB; passed as `--cachesize -2000` |
| `page_size` | `4096` | Database page bytes |
| `jmode` | `"delete"` | Common rollback-journal NAS baseline |
| `nosync` | `False` | FULL synchronization, pinned by the build |
| `repeats` | `5` | Complete suite invocations, each on a fresh database |
| `build_dir` | `None` | Default `build/sqlite-speedtest/` |
| `force_download` | `False` | Refresh sources and rebuild when True |

Each invocation runs all official main stages in order. Several write stages use
one transaction for many statements; this is not one commit per statement. Rows
carry the scale, page/cache size and journal/sync settings as key columns.
Sorting helper
threads are fixed to zero, mmap stays disabled, and exclusive locking is not
requested. `--threads` would control sorting workers, not concurrent clients;
`--multithread` would select SQLite's threading mode, not launch SQL clients.

The first run needs a C compiler and network access. Shared `sqlite_tools.py`
records source/binary hashes, compiler version and compile argv in
`speedtest1-build.json`; reusable builds must also match current compile flags.
Downloads must match the fixed SHA-256 values in `sqlite_tools.py` before cache
replacement; cached archives and test sources are also verified before reuse.
The expected values were cross-checked against the official 3.54.1 release
archives and their [published SHA3-256](https://sqlite.org/download.html).
Both SQLite tools use `-O2`, `SQLITE_THREADSAFE=1`, no loadable extensions, and
`SQLITE_DEFAULT_SYNCHRONOUS=2` / `SQLITE_DEFAULT_WAL_SYNCHRONOUS=2`. Builds and
result writes are outside the measured invocation. The official source is not
patched and the system/Python SQLite library is not the measured engine.

## Results and timing boundaries

The run writes two flat tables:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `stages` | `jmode`, `sync` (`full`/`off`), `size`, `page_size`, `cache_size_kib`, `stage` (official stage ID), `description`, `repeat` | `elapsed_s` |
| `totals` | `jmode`, `sync`, `size`, `page_size`, `cache_size_kib`, `repeat` | `official_total_s`, `wall_s` |

`summary/stages.csv` and `summary/totals.csv` pool repeats per target and
setting and report `n` and each metric's median. The manifest's
`targets[].info` holds the pinned SQLite version, compiler version and
source/binary hashes.

`official_total_s` is speedtest1's printed TOTAL: the sum of its stage timers.
`wall_s` covers process launch through exit, including connection setup, work
outside stage timers, output and final database close. These metrics are not
interchangeable. In the pinned source, optional explicit `--checkpoint` runs
**after** the stage elapsed time is captured, and final close follows TOTAL.
This runner does not enable that option. Never describe TOTAL as including all
checkpoint/close work.

Official timers have millisecond resolution; smoke-scale stages can print zero.
Use a fixed larger scale for the paper rather than interpreting these as
zero-cost operations. Missing main stages or inconsistent TOTAL output fail the
measurement. Requested PRAGMAs remain requests: speedtest1 does not print their
connection-level readbacks. The main suite includes an `integrity_check` stage,
but its presence is not a crash-consistency or successful-commit proof.

Databases remain under
`<target>/agent-fs-bench/sqlite-speedtest/<run-id>/repeat-N/speedtest.sqlite`.
The default figures are a box plot of `official_total_s` per target (x = journal
mode) and a line of median `elapsed_s` by stage ID with target series;
`uv run python -m agent_fs_bench plot <run-dir>...` re-renders them. Source and
binary fingerprints identify the actual official build.

## NAS comparison and paper figures

Use DELETE + FULL consistently across targets. Do not use WAL as the generic
NAS baseline: see [SQLite over a network](https://sqlite.org/useovernet.html) and
[WAL restrictions](https://sqlite.org/wal.html). Successful smoke runs do not prove
correct filesystem locks, synchronization or power-loss durability. Rollback
FULL is not an unconditional power-loss durability guarantee; see
[`synchronous`](https://sqlite.org/pragma.html#pragma_synchronous).

Keep client, network, build, scale and cache policy fixed; rotate target order
between full experiment repetitions. No caches are evicted here. A fresh DB or
SQLite cache smaller than the DB does not establish cold client/server caches.
Different server hardware means an end-to-end NAS comparison, not isolated
filesystem-software overhead.

Default paper views:

1. `totals` box plot of `official_total_s` per target: complete-suite time.
2. `stages` line plot of median `elapsed_s` by stage ID, with NAS targets as
   series. Run the complete suite; select stages only for display with
   `plot --table stages --x stage --y elapsed_s --where stage=240 ...`. Keep
   their different SQL and transaction semantics explicit instead of deriving
   one mixed ops/s number.
3. `kvtest` amortized microseconds/update versus fixed BLOB size, with separate
   autocommit and batch panels. Do not derive transaction p95/p99 from run means.

Each sample is a complete run's stage time, not an individual SQL latency.
Select only compatible runs: `plot` refuses run directories whose configs
differ. No same-database concurrency curve is claimed. Stock `kvtest` can report
failed commits as successful work under contention; do not parallelize it on
one DB.
