# mdtest

Measure POSIX metadata throughput with the unmodified **IOR mdtest 4.0.0** at one task count N per run. No fio, data-bandwidth test, cache eviction, extra fsync, automatic installation or retry.

## Prerequisites and commands

Prepare `mdtest` 4.0.0 linked against the Open MPI 4.1 of every task machine, outside this repository. The [official release](https://github.com/hpc/ior/releases/tag/4.0.0) contains build instructions. Every machine must run the same binary: the run fails when tasks report different SHA-256 digests. Open MPI does not read shell profiles on remote machines, so prefer an absolute `mdtest_bin`. Mount targets externally; machine placement follows [run modes](run-modes.md).

```sh
uv sync --locked

# One task count per run (CONFIG["tasks"]); --mode picks the machines.
uv run python -m agent_fs_bench run mdtest --mode local --dirs /mnt/fs-a /mnt/fs-b

# Small real end-to-end check, not a performance result.
uv run python -m agent_fs_bench run mdtest --mode local --dirs /mnt/smoke -- --smoke
```

## Experiment settings

Edit the single `CONFIG` dictionary in `agent_fs_bench/benchmarks/mdtest.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `mdtest_bin` | `mdtest` | Native IOR 4.0.0 executable, the same on every machine |
| `tasks` | `1` | N single-threaded mdtest tasks of this run |
| `cpus_per_task`, `oversubscribe` | `1`, `1` | One pinned logical CPU per task, never shared |
| `total_items` | 65536 | Fixed aggregate objects in each phase, not objects per task |
| `repeats` | 5 | Independent launches with fresh working directories |
| `layout` | `private` | `private`: one directory per machine with per-task trees (`-u`); `shared`: one common directory |
| `subset` | `file` | `file` or `dir`, one per run |

Each launch uses `-a POSIX -i 1 -n total_items/N`, with `-F` or `-D`. Total work must divide evenly and leave at least two items per task.
- **Private layout:** the tasks of one machine share one `-d` directory `<work>/<node>/<case>` plus `-u`, so each task creates its own `mdtest_tree.<rank>.0` there, as in a single-machine mdtest run. mdtest creates its `test-dir.0-0` only for rank 0, so each machine precreates its own.
- **Shared layout:** every task gets one `-d` directory `<work>/<case>`, still with rank-unique names, **not** `-S` shared-file access. Across machines the experimenter must provide a target that every machine sees as one directory.

Directory-tree management remains outside the reported operation phases. `--smoke` changes only total objects to 128 and repeats to 2.

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
| `phases` | `operation`, `layout`, `tasks`, `repeat` | `ops`, `elapsed_s`, `ops_per_s` |

One row is one operation at one layout, task count and repeat. `run` also writes `summary/phases.csv` (n and the median of each metric per target and dimension). The manifest holds the config, the mdtest version, the cache/completion policy, the executable SHA-256 proved by every task, and every launch's verified task placement.

`elapsed_s` comes from the CSV `all` row's aggregate phase time, including the end barrier: the longest phase time across tasks, not subprocess startup or an average of task times. `ops` is the fixed total verified against mdtest's own workload report; `ops_per_s = ops / elapsed_s`.

**IOR 4.0.0 CSV caveat:** its result-copy loop writes beyond `time_before_barrier[9]` and overwrites aggregate directory create/stat item counts with rank 0's local counts. Thus those CSV `rate-*` values undercount by the task count. The wrapper consistently uses fixed total work divided by aggregate time for **every** operation, never CSV rates or the per-task `items` column. See the [result structure](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.h) and [aggregation/export implementation](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c). No upstream binary patch or version fallback is used.

Before emitting a phase row, the wrapper checks:
- the reported version;
- the task count, and the machine count of the placement;
- the workload size;
- the exit status and operation warnings.

The known zero-byte read notice is expected. Launcher logs stay outside the target under the result directory. Task 0's machine returns the rank CSV through the launcher output, so no machine needs a shared result directory. mdtest removes the files and directories it creates; the empty per-repeat directories, and the precreated `test-dir.0-0` on every machine but task 0's, stay under the run directory. A failed launch keeps its diagnostics and stops the run.

Cache state is **create-then-access-no-drop**, not guaranteed warm residency or cold NAS access. Stat/open follow creation without cache eviction; metadata may be served by client caches. Completion is ordinary operation return: no extra `-y`/`-Y`, no per-operation tail latency or crash-durability claim.

## Default figures

`run` renders one figure per operation/layout, e.g. `figures/phases-ops_per_s-file_create-private.pdf` (punctuation in split values becomes `_`), plus a standalone Python script with the raw samples. X is the task count (log scale); Y is Kops/s. One run gives one x point; pool runs of different `tasks` to draw the curve. Lines show the median and IQR across independent repeats; these are throughput variation, **not operation-latency percentiles**.

```sh
# Pool one configuration at several task counts.
uv run python -m agent_fs_bench plot results/mdtest/<run-n1> results/mdtest/<run-n16> \
  --vary tasks --targets ours,nfs,ext4
```

`plot` accepts several run directories of the same benchmark and rejects runs whose config or source fingerprint differ, except in keys given to `--vary`. `--targets` sets legend order and filtering; the first entry is the paper's primary system. A custom figure uses `--table phases --x tasks --y ops_per_s` with optional `--split`, `--where`, `--xlabel` and `--ylabel`.

For the paper, prioritize create/stat/unlink curves for private and shared directories; keep the remaining operations as supplemental results. Describe local filesystems as local references, not resource-equivalent NAS baselines. Input series must use the same total work, mdtest version, cache/completion policy and mode; report the machines and placement of each point.

A full smoke writes **8 `phases` rows** (4 file operations × 2 repeats); `summary/phases.csv` then holds 4 groups with n and the median of each metric. Its two repeats and tiny phases prove the execution/analysis path only; choose a sufficiently long common work size before collecting paper results.
