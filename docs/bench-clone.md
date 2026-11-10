# Durable directory clone benchmark

Compares independent writable directory copies within one externally mounted
filesystem: standard `cp -r` on each target, optionally the native
`lakebase-client clone` command on LakeBase. Linux and Python 3.12+ are required.
The benchmark never mounts targets, retries operations, or substitutes a copier.
Only generated regular files and directories are in scope; this is not an
archive-metadata or concurrent-source-mutation benchmark.

Each run measures **one** directory shape (`files` × `file_size_bytes`) at **one**
external concurrency (`tasks`). Every task issues one complete-tree clone request
on its own machine, and one global Open MPI job starts all requests together.
Curves over tasks come from separate runs pooled with `plot --vary tasks`.

## Run and configure

The workload CLI exposes only `--smoke`:

```sh
uv run python -m agent_fs_bench run clone --mode local --dirs /mnt/fs-a -- --smoke

# Formal experiment: edit CONFIG first, then omit --smoke.
uv run python -m agent_fs_bench run clone --mode local --dirs /mnt/fs-a
```

`--mode local` runs every task on the controller; `overflow` fills the controller
first and then the machines in `local/hosts.toml` in file order; `remote` uses
only those machines and hosts no task on the controller. Every machine needs the
same checkout and its own `uv sync --locked` environment.

`CONFIG` in `agent_fs_bench/benchmarks/clone.py` holds all settings. Defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `files` | `10000` | Files in one complete directory request. |
| `file_size_bytes` | `4096` | Bytes per file; `files × file_size_bytes` is the request size. |
| `files_per_dir` | `128` | Consecutive files per generated `d_i` directory. |
| `repeats` | `5` | Independent fresh-destination batches per copier. |
| `tasks` | `1` | Simultaneous independent requests, one per task process. |
| `cpus_per_task` | `1` | Logical CPUs reserved per task; `cp` is single-threaded. |
| `oversubscribe` | `1` | Tasks per CPU group. |
| `copiers` | `["cp-r"]` | Default ordered copier names for each target. |
| `target_copiers` | `{}` | Explicit target-alias overrides, including multiple operations on one filesystem. |
| `commands` | `cp-r` and `lakebase` templates | Shell-quoted argv containing `{src}` and `{dst}`; executed without a shell. Only selected executables must exist. |
| `keep_scratch` | `False` | Retain generated source and destination trees for inspection. |

`tasks` is the only concurrency control; there is no in-run sweep of `files`,
`file_size_bytes`, `tasks` or copiers. A run with `tasks=16` starts 16 identical
requests, split across the participating machines by the placement plan. A task
count above the probed capacity of the selected mode is an error, never a smaller
run.

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
selection, command templates, layout, `tasks` and retention:

- 32 files of 4096 B, one empty directory, and two repeats.
- `batches.csv` gets one row per copier × repeat; `requests.csv` gets `tasks`
  rows per batch.

At most 128 KiB of source payload is present per node in a smoke case. The
conservative free-space check includes a 256 MiB margin. No formal-size
benchmark is needed to check correctness or plotting.

## Workload and timing protocol

Every machine generates its own source tree; no directory is shared between
machines and no machine reads data prepared on another machine.

1. **Per node, on the leader task** (outside timing): resolve the selected copier
   executables on that machine and validate each once in a tiny scratch fixture
   (full content, empty directories, bidirectional source/destination write
   independence); check free space for `local_count` destination copies plus the
   source; generate and sync one source tree in `t.node_dir`; record the
   filesystem type and copier executable SHA-256 digests in
   `targets[].info.hosts[<node>]`; then wait at the node barrier.
2. **Per batch** (outside timing): every task removes its previous destination
   and creates a fresh destination parent in its own `t.task_dir`; every task
   prereads its node's source tree.
3. `t.batch(...)` releases all tasks together. Each task runs:
   command → destination files/directories synced bottom-up → destination
   parent synced. The destination itself must initially be absent. Batch wall
   time is measured between the two MPI barriers, so thread/task startup and
   shutdown, verification, emission and cleanup are outside time.
4. Every task verifies its own copied tree against the node's source inventory
   (structure and sizes; contents were checked during validation).
5. Copier order rotates between repeats, so no copier is always first.

There is no `threads` parameter: one task runs one external copy command, and
`tasks` counts independent commands. This measures simultaneous template clones,
not internal parallelization of a single `cp` command.

A successful `fsync` is the persistence contract for this experiment, including
NFS/FUSE implementations. No crash-recovery or remote failure guarantee is
claimed beyond that assumption. Recursive sync includes directory walking and
file open/close overhead: the main result is **durable directory clone latency**,
not pure metadata-clone API latency.

Every repeat prereads the source, but this neither pins data nor clears FUSE,
NAS or server caches. Call the state `source-preread-every-repeat`, not cold or
guaranteed fully warm. Use datasets that fit the intended warm-working-set
scenario; client memory and remote caches remain experimental conditions.

Unless `keep_scratch` is set, each node directory (source, scratch and
destinations) is removed after the run; only the empty run directory remains. A
failed run aborts the job and leaves the trees for inspection. Noisy command
output goes to the job's stderr log; per node diagnostics live under
`results/clone/<run-id>/<target>/hosts/<node>/` in each machine's checkout.

## Results

The runner writes `results/clone/<run-id>/`:

- `manifest.json`: configuration and per-node facts under
  `targets[].info.hosts[<node>]`, including the filesystem type and copier
  executable SHA-256 digests.
- `batches.csv`: one row per target × copier × repeat, for the run's one shape.
- `requests.csv`: one row per task request within a batch.
- `summary/batches.csv`, `summary/requests.csv`: `n` and median metrics per
  target × dimension.
- `figures/`: default figures, described below.

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `batches` | `target`, `copier`, `files`, `file_size_bytes`, `total_bytes`, `tasks`, `repeat` | `wall_s`, `clones_s` |
| `requests` | the `batches` keys plus `sample`, `host` | `copy_s`, `fsync_s`, `latency_s` |

`files` and `file_size_bytes` describe one complete directory request,
`total_bytes` is their product, and `tasks` is the number of simultaneous
requests in the batch. `repeat` is 1-based; `sample` is the task index and
`host` is the node that ran it. The first row fixes a table's columns.

Metrics:

| Metric | Meaning |
| --- | --- |
| `wall_s` | Barrier release through last task completion. |
| `clones_s` | Completed directories / batch wall time. Finite-batch throughput, not a steady-state offered-load test. |
| `latency_s` | One request's command-plus-sync latency. |
| `copy_s`, `fsync_s` | One request's command and sync phases; they sum to its `latency_s`. |

`summary/batches.csv` has one row per target × copier with `n` repeats;
`summary/requests.csv` pools every request sample across repeats. Use
request-level summaries for latency and batch-level summaries for wall time and
throughput. Per-request latency starts immediately before command execution;
batch time also includes barrier wake-up and the slowest task's completion. Do
not interpret MiB/s as physical bandwidth for CoW clone; the primary metrics are
latency and directories per second.

A `target/copier` pair identifies a series, e.g. `LakeBase/lakebase`,
`LakeBase/cp-r`, `NFS/cp-r`; operations cannot merge into one filesystem row.
The `plot` command pools several runs only when their source fingerprints and
manifest configurations match apart from `--vary`. Because one run holds one
shape and one `tasks` value, draw scaling curves from several runs and pass the
varying keys explicitly.

## Default figures

Every run renders the module's `FIGURES` into `figures/` as vector PDF, 600 dpi
PNG, and a standalone Python script with the raw samples. A figure is split into
one panel per distinct value of its `split` columns, so runs of different shapes
never share a line.

| Figure | Table | x | y | Split |
| --- | --- | --- | --- | --- |
| `latency-tasks` | `requests` | `tasks` | `latency_s` | `files`, `file_size_bytes`, log-log |
| `throughput-tasks` | `batches` | `tasks` | `clones_s` | `files`, `file_size_bytes` |
| `latency-bytes` | `requests` | `total_bytes` (MiB) | `latency_s` | `files`, `tasks`, log-log |
| `latency-files` | `requests` | `files` | `latency_s` | `file_size_bytes`, `tasks`, log-log |

Lines show medians with IQR error bars. Series are `target/copier`, ordered by
target. A figure holds at most 4 series; `--targets` selects and orders them by
target (`LakeBase`) or full series label (`LakeBase/lakebase`), putting the
paper's primary system first, and rejects names that match nothing.

Pool one run per task count for a scaling curve:

```sh
uv run python -m agent_fs_bench plot results/clone/<run-tasks1> results/clone/<run-tasks8> \
  --vary tasks
```

Pool runs that vary file size at a fixed file count for `latency-bytes`
(`--vary file_size_bytes`), and runs that vary file count at a fixed file size
for `latency-files` (`--vary files`). Add the other varying keys to `--vary`
when a pool changes several dimensions at once; `plot` rejects pools whose
remaining configuration differs. A single run therefore holds one x point per
figure; the tasks figures need pooled runs.

Recommended main comparisons: `latency-tasks` with `throughput-tasks` for
concurrency scaling, `latency-bytes` and `latency-files` for shape sensitivity.
Tiny smoke samples do not support tail-latency claims.

Style reuses the project Arial 9pt palette, filled markers, boxed axes, top
legend and no internal grids. Default single-column size is 3.5 × 1.5 inches;
use `--size` only if a specific paper layout requires it. Standalone scripts
regenerate PDF/PNG without the installed benchmark package.
