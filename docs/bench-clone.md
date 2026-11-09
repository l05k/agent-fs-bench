# Durable directory clone benchmark

Compares independent writable directory copies within one externally mounted
filesystem: standard `cp -r` on each target, optionally the native
`lakebase-client clone` command on LakeBase. Linux and Python 3.12+ are required.
The benchmark never mounts targets, retries operations, or substitutes a copier.
Only generated regular files and directories are in scope; this is not an
archive-metadata or concurrent-source-mutation benchmark.

## Run and configure

The workload CLI still exposes only `--smoke`:

```sh
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a -- --smoke

# Formal experiment: edit CONFIG first, then omit --smoke.
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a
```

`CONFIG` in `agent_fs_bench/benchmarks/clone.py` holds all settings. Defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `stage` | `"all"` | `bytes`, `files`, `concurrency`, or all three. |
| `repeats` | `5` | Independent fresh-destination batches at each point. |
| `copiers` | `["cp-r"]` | Default ordered copier names for each target. |
| `target_copiers` | `{}` | Explicit target-alias overrides, including multiple operations on one filesystem. |
| `commands` | `cp-r` and `lakebase` templates | Shell-quoted argv containing `{src}` and `{dst}`; executed without a shell. Only selected executables must exist. |
| `byte_totals` | 16, 64, 256 MiB; 1, 4 GiB | Total bytes per directory in the byte sweep. |
| `byte_files` | `16` | Fixed file count for byte sweep and large concurrency workload; totals must divide evenly. |
| `file_counts` | `100, 1000, 10000, 100000` | File-count sweep points. |
| `small_file_size` | `4096` | Bytes per file in file-count and small concurrency workloads. |
| `conc_levels` | `1, 2, 4, 8, 16` | Concurrent full-directory requests. |
| `concurrency_large_bytes` | 256 MiB | Bytes in **each** large concurrency request. |
| `concurrency_small_files` | `10000` | Files in **each** small concurrency request. |
| `files_per_dir` | `128` | Consecutive files per generated `d_i` directory. |
| `keep_scratch` | `False` | Retain generated benchmark trees for inspection. |

Example with registered aliases `LakeBase` and `NFS`:

```python
CONFIG["copiers"] = ["cp-r"]
CONFIG["target_copiers"] = {"LakeBase": ["lakebase", "cp-r"]}
CONFIG["commands"] = {
    "cp-r": "cp -r {src} {dst}",
    "lakebase": "lakebase-client clone {src} {dst}",
}
```

All other targets use `cp-r`. Running both operations on LakeBase separates
native-clone benefits from ordinary filesystem access performance. Use exact
registered target names, or the directory basenames when using `--dirs`; only
selected copiers need installed executables. Before creating a run, every
`target_copiers` key is checked case-sensitively against the complete target
catalog (or all `--dirs` basenames). An unknown key is an error, never an
implicit switch to `cp-r`; the check precedes `--target` filtering, so valid
overrides for unselected targets are allowed.

A command must return only when the copied tree is complete and available to
filesystem operations; a command that only submits an asynchronous job is not
suitable without a wrapper that waits for its completion.

The default native command does not request archive attributes. The experiment
requires equal file contents, directory structure, and independent subsequent
writes, not ownership/timestamps/ACL/xattr/hard-link preservation. See the
[LakeBase directory clone interface](https://help.aliyun.com/zh/polardb/polardb-for-mysql/polardb-agent-lakebase-directory-clone).
GNU `cp -r` may use reflink or copy offload; these standard optimizations remain
enabled. Keep the executable and mount configuration fixed and inspect its
actual path outside measurement when interpreting results.

### Small smoke, not a performance experiment

`--smoke` overrides scale and repeats, while retaining configured copier
selection, command templates, layout, and retention:

- Byte sweep: 1 and 4 MiB, each with four files.
- File sweep: 8 and 32 files, each 4 KiB.
- Concurrency: 1 and 4 complete requests, using a four-file 4 MiB directory and
  a 32-file 4 KiB directory.
- Two repeats: 16 rows in `batches.csv` and 28 in `requests.csv` per copier.

At most 4 MiB of source payload and 16 MiB of destination payload are present
in a smoke case. The conservative free-space check includes a 256 MiB margin.
No formal-size benchmark is needed to check correctness or plotting.

## Workload and timing protocol

The byte sweep fixes file count/layout while varying file size. The file sweep
fixes file size and groups consecutive files into the same `files_per_dir`
layout rule. Each source also includes an empty directory. Random data is
written in bounded chunks; zero/sparse inputs are deliberately not a second
experimental mode.

Each concurrency request copies the **entire identical source** into a private
`request_i/tree`. Increasing concurrency does not split the dataset or change
its per-request file count/size. This measures simultaneous template clones,
not internal parallelization of a single `cp` command. There is no `threads`
parameter; thread-pool workers only launch independent external commands and
perform their corresponding sync traversal.

1. Prepare and sync the run namespace; generate and sync source files,
   directories bottom-up, and the source parent outside timing.
2. Once per selected copier/target, use a tiny separate fixture to verify full
   content, empty directories, and bidirectional source/destination write
   independence. Hard-linked or incorrect-content copies fail before results.
3. Before **each** copier/repeat, remove old destinations, create private
   destination parents and sync them and their namespace. Preread the source
   after preparation, outside timing.
4. For concurrent cases, create all worker threads and wait at a common start
   barrier before starting the batch clock. Each worker then executes:
   command → destination files/directories synced bottom-up → destination
   parent synced. The destination itself must initially be absent.
5. End batch wall time at the last request completion. Thread shutdown,
   structural/size verification, result emission and cleanup are outside time.
   Copier order rotates between repeats.

A successful `fsync` is the persistence contract for this experiment, including
NFS/FUSE implementations. No crash-recovery or remote failure guarantee is
claimed beyond that assumption. Recursive sync includes directory walking and
file open/close overhead: the main result is **durable directory clone latency**,
not pure metadata-clone API latency.

Every repeat prereads the source, but this neither pins data nor clears FUSE,
NAS or server caches. Call the state `source-preread-every-repeat`, not cold or
guaranteed fully warm. Use datasets that fit the intended warm-working-set
scenario; client memory and remote caches remain experimental conditions.

## Results

The runner writes `results/clone/<run-id>/`:

- `manifest.json`: configuration and per-target facts, including the filesystem
  type and copier executable SHA-256 digests.
- `batches.csv`: one row per target × copier × workload × repeat.
- `requests.csv`: one row per individual request within a batch.
- `summary/batches.csv`, `summary/requests.csv`: `n` and median metrics per
  target × dimension.
- `figures/`: default figures, described below.

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `batches` | `target`, `copier`, `experiment`, `kind`, `files`, `file_size_bytes`, `total_bytes`, `concurrency`, `repeat` | `wall_s`, `clones_s` |
| `requests` | the `batches` keys plus `sample` | `copy_s`, `fsync_s`, `latency_s` |

`experiment` is `bytes`, `files` or `concurrency`; `kind` is `large` or `small`.
`files` and `file_size_bytes` describe one complete directory request,
`total_bytes` is their product, and `concurrency` is the number of simultaneous
requests in the batch. `repeat` is 1-based; `sample` indexes the request within
its batch. The first row fixes a table's columns.

Metrics:

| Metric | Meaning |
| --- | --- |
| `wall_s` | Barrier release through last completion; excludes worker creation/shutdown. |
| `clones_s` | Completed directories / batch wall time. Finite-batch throughput, not a steady-state offered-load test. |
| `latency_s` | One request's command-plus-sync latency. |
| `copy_s`, `fsync_s` | One request's command and sync phases; they sum to its `latency_s`. |

`summary/batches.csv` has one row per target × copier × workload with `n`
repeats; `summary/requests.csv` pools every request sample across repeats. Use
request-level summaries for latency and batch-level summaries for wall time and
throughput. Per-request latency starts immediately before command execution;
batch time also includes barrier wake-up/scheduling. Do not interpret MiB/s as
physical bandwidth for CoW clone; the primary metrics are latency and
directories per second.

A `target/copier` pair identifies a series, e.g. `LakeBase/lakebase`,
`LakeBase/cp-r`, `NFS/cp-r`; operations cannot merge into one filesystem row.
The `plot` command pools several runs only when their source fingerprints and
manifest configurations match. This is a clean cutover from the old sharded
format: remeasure old clone runs instead of combining their rows with new results.

## Default figures

Every run renders the module's `FIGURES` into `figures/` as vector PDF, 600 dpi
PNG, and a standalone Python script with the raw samples:

| Figure | Table | x | y | Filter |
| --- | --- | --- | --- | --- |
| `latency-bytes` | `requests` | `total_bytes` (MiB) | `latency_s` | `experiment=bytes`, log-log |
| `latency-files` | `requests` | `files` | `latency_s` | `experiment=files`, log-log |
| `latency-concurrency-large` | `requests` | `concurrency` | `latency_s` | `experiment=concurrency`, `kind=large`, log-log |
| `latency-concurrency-small` | `requests` | `concurrency` | `latency_s` | `experiment=concurrency`, `kind=small`, log-log |
| `throughput-large` | `batches` | `concurrency` | `clones_s` | `experiment=concurrency`, `kind=large` |
| `throughput-small` | `batches` | `concurrency` | `clones_s` | `experiment=concurrency`, `kind=small` |

Lines show medians with IQR error bars. Series are `target/copier`, ordered by
target. A figure holds at most 4 series; `--targets` selects and orders them by
target (`LakeBase`) or full series label (`LakeBase/lakebase`), putting the
paper's primary system first, and rejects names that match nothing. Re-render with:

```sh
uv run python -m agent_fs_bench plot results/clone/<run-id>
```

Recommended main comparisons: byte/file sensitivity using `latency_s`, and
large/small concurrency using `clones_s` together with `latency_s`. Tiny smoke
samples do not support tail-latency claims.

Style reuses the project Arial 9pt palette, filled markers, boxed axes, top
legend and no internal grids. Default single-column size is 3.5 × 1.5 inches;
use `--size` only if a specific paper layout requires it. Standalone scripts
regenerate PDF/PNG without the installed benchmark package.
