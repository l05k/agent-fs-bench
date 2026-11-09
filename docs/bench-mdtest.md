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

Each run writes one flat CSV per record kind plus a `manifest.json`:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | `operation`, `layout`, `procs`, `repeat` | `ops`, `elapsed_s`, `ops_per_s` |

One row is one operation at one layout, process count and repeat. `run` also writes `summary/phases.csv` (n and the median of each metric per target and dimension) and puts the config, mdtest version, cache/completion policy, resolved `mdtest`/MPI paths and the executable SHA-256 in `manifest.json`.

`elapsed_s` comes from the CSV `all` row's aggregate phase time, including the end barrier: the longest phase time across ranks, not subprocess startup or an average of rank times. `ops` is the fixed total verified against mdtest's own workload report; `ops_per_s = ops / elapsed_s`.

**IOR 4.0.0 CSV caveat:** its result-copy loop writes beyond `time_before_barrier[9]` and overwrites aggregate directory create/stat item counts with rank 0's local counts. Thus those CSV `rate-*` values undercount by the process count. The wrapper consistently uses fixed total work divided by aggregate time for **every** operation, never CSV rates or the per-process `items` column. See the [result structure](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.h) and [aggregation/export implementation](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c). No upstream binary patch or version fallback is used.

The wrapper checks the reported version, process count, single-node execution, workload size, exit status and operation warnings before emitting a phase row. The known zero-byte read notice is expected. Logs stay outside the target under the result directory; successfully parsed CSV and working trees are removed. Failed launches leave diagnostics and stop the run.

Cache state is **create-then-access-no-drop**, not guaranteed warm residency or cold NAS access. Stat/open follow creation without cache eviction; metadata may be served by client caches. Completion is ordinary operation return: no extra `-y`/`-Y`, no per-operation tail latency or crash-durability claim.

## Default figures

`run` renders one figure per operation/layout, e.g. `figures/phases-ops_per_s-file-create-private.pdf`, plus a standalone Python script with the raw samples. X is client process count (log scale); Y is Kops/s. Lines show the median and IQR across independent repeats; these are throughput variation, **not operation-latency percentiles**.

```sh
# Re-render the default figures for a finished run (or pool runs of one config).
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --targets ours,nfs,ext4
```

`plot` accepts several run directories of the same benchmark and rejects configs that differ. `--targets` sets legend order and filtering; the first entry is the paper's primary system. A custom figure uses `--table phases --x procs --y ops_per_s` with optional `--split`, `--where`, `--xlabel` and `--ylabel`.

For the paper, prioritize create/stat/unlink curves for private and shared directories; keep the remaining operations as supplemental results. Describe local filesystems as local references, not resource-equivalent NAS baselines. Input series must use the same total work, mdtest version, cache/completion policy and process counts. Mounts, hardware and server settings remain the experimenter's responsibility.

A full smoke writes **64 `phases` rows** (8 operations × 2 layouts × 2 process counts × 2 repeats); `summary/phases.csv` then holds 32 groups with n and the median of each metric. Its two repeats and tiny phases prove the execution/analysis path only; choose a sufficiently long common work size before collecting paper results.
