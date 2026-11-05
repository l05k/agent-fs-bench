# Single-client mdtest

Measure baseline POSIX metadata throughput with the unmodified **IOR mdtest 4.0.0**. One Linux client runs multiple MPI processes. No fio, data-bandwidth test, cache eviction, extra fsync, automatic installation or retry.

## Prerequisites and commands

Prepare `mdtest` 4.0.0 and its matching MPI runtime outside this repository; both `mdtest` and `mpiexec` must be on PATH. The [official release](https://github.com/hpc/ior/releases/tag/4.0.0) contains build instructions. Use the same binary/runtime across targets. The launcher must start all ranks on one node; its extra options belong in `CONFIG["mpi_command"]`, before the final process-count flag. Mount targets externally.

```sh
uv sync --locked

# Formal fixed-work sweep; uses the mounted target directory.
uv run python -m agent_fs_bench run mdtest --dirs /mnt/fs-a /mnt/fs-b

# Small real end-to-end check, not a performance result.
uv run python -m agent_fs_bench run mdtest --dirs /mnt/smoke -- --smoke
```

## Experiment settings

Edit the single `CONFIG` dictionary in `agent_fs_bench/benchmarks/mdtest.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `mdtest_bin` | `mdtest` | Native IOR 4.0.0 executable |
| `mpi_command` | `("mpiexec", "-n")` | Launcher prefix followed by the process-count flag |
| `total_items` | 65536 | Fixed aggregate objects in each phase, not objects per process |
| `conc_levels` | `(1, 2, 4, 8, 16)` | Processes on the single client |
| `repeats` | 5 | Independent launches with fresh working directories |
| `layouts` | `("private", "shared")` | Per-process parent directories versus a common parent |
| `subsets` | `("file", "dir")` | Files and directories, measured in separate launches |

Each launch uses `-a POSIX -i 1 -n total_items/P`, with `-F` or `-D`. Private layout adds `-u`; shared layout omits it but still uses rank-unique names, **not** `-S` shared-file access. Total work must divide evenly and leave at least two items per process. Directory-tree management remains outside the reported operation phases. `--smoke` changes only total objects to 128, process counts to `(1, 2)` and repeats to 2.

| Operation | Timed work |
| --- | --- |
| `file-create` | Create an empty file and close it |
| `file-stat` | Stat each created file |
| `file-open` | Open/close; mdtest's zero-byte `File read`, not data reading |
| `file-unlink` | Unlink each file |
| `dir-mkdir` | Create empty directories |
| `dir-stat` | Stat each created directory |
| `dir-rename` | mdtest's directory-name rotation; not general file rename |
| `dir-rmdir` | Remove empty directories |

`Directory read` is an upstream no-op and is never imported. Tree creation/removal are also excluded. No directory listing (`readdir`) is measured.

## Timing and results

One JSONL row is one operation/layout/process-count/repeat, for example `file-create/private/N65536/P8`. Metrics are `concurrency`, `ops`, `elapsed_s` and `ops_per_s`; `details` records operation/layout. Parameters retain the work sizes, tool version, executable SHA-256, launcher, cache state and completion boundary. Successful runs generate the existing `summary.csv` automatically.

`elapsed_s` comes from the CSV `all` row's aggregate phase time, including the end barrier: the longest phase time across ranks, not subprocess startup or an average of rank times. `ops` is the fixed total verified against mdtest's own workload report; `ops_per_s = ops / elapsed_s`.

**IOR 4.0.0 CSV caveat:** its result-copy loop writes beyond `time_before_barrier[9]` and overwrites aggregate directory create/stat item counts with rank 0's local counts. Thus those CSV `rate-*` values undercount by the process count. The wrapper consistently uses fixed total work divided by aggregate time for **every** operation, never CSV rates or the per-process `items` column. See the [result structure](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.h) and [aggregation/export implementation](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c). No upstream binary patch or version fallback is used.

The wrapper checks the reported version, process count, single-node execution, workload size, exit status and operation warnings before emitting the case. The known zero-byte read notice is expected. Logs stay outside the target under the result directory; successfully parsed CSV and working trees are removed. Failed launches leave diagnostics and stop the run.

Cache state is **create-then-access-no-drop**, not guaranteed warm residency or cold NAS access. Stat/open follow creation without cache eviction; metadata may be served by client caches. Completion is ordinary operation return: no extra `-y`/`-Y`, no per-operation tail latency or crash-durability claim.

## Paper figures

```sh
uv sync --locked --group analysis
uv run --group analysis python -m agent_fs_bench plot \
  'results/mdtest/<run-id>/results.jsonl' \
  --benchmark mdtest --metric ops_per_s \
  --targets ours,nfs,ext4 --output results/mdtest-metadata
```

The plot command writes one numeric-x curve per operation/layout, e.g. `mdtest-metadata-file-create-private.pdf`, plus 600 dpi PNG and a self-contained Python script with the raw repeat samples. Default size is 3.5 × 1.5 inches. X is client process count (log scale); Y is Kops/s. Curves show median and IQR across independent runs; these are throughput variation, **not operation-latency percentiles**. The first `--targets` entry is the paper's primary system.

For the paper, prioritize create/stat/unlink curves for private and shared directories; keep the remaining operations as supplemental results. Describe local filesystems as local references, not resource-equivalent NAS baselines. Input series must use the same total work, mdtest version, cache/completion policy and process counts; plotting rejects incompatible selections. Mounts, hardware and server settings remain the experimenter's responsibility. `--cases` selects exact cases without changing numeric x order; `--ylabel` changes the label only, not the Kops/s scaling.

A full smoke emits **64 rows** (8 operations × 2 layouts × 2 process counts × 2 repeats), then produces 128 summary rows (4 metrics × 32 cases). Its two repeats and tiny phases prove the execution/analysis path only; choose a sufficiently long common work size before collecting paper results.
