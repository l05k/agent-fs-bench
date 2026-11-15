# Durable directory clone benchmark

Compares independent writable directory copies within one externally mounted
filesystem: GNU `cp -r --reflink=never` on each target, optionally the native
Alibaba Cloud PolarDB OpenAPI `ClonePolarFsBasicSnapshot` operation on LakeBase.
Linux and Python 3.12+ are required. The benchmark never mounts targets, retries
operations, falls back from the selected copier, or substitutes one. Only
generated regular files and directories are in scope; this is not an
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

`--mode local` runs every task on the controller; `overflow` deals tasks round-robin
over the controller and the machines in `local/hosts.toml` in file order; `remote` uses
only those machines and hosts no task on the controller. Every machine needs the
same checkout and its own `uv sync --locked` environment.

`CONFIG` in `agent_fs_bench/benchmarks/clone.py` holds all settings. Defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `files` | `10000` | Files in one complete directory request. |
| `file_size_bytes` | `4096` | Bytes per file; `files × file_size_bytes` is the request size. |
| `files_per_dir` | `128` | Consecutive files per generated `d_i` directory. |
| `repeats` | `5` | Independent fresh-destination batches. |
| `tasks` | `1` | Simultaneous independent requests, one per task process. |
| `copier` | `"cp-r"` | Copier for every target without an override: `cp-r` or `polarfs`. |
| `target_copiers` | `{}` | Target alias → copier name override; one copier per target and run. |
| `polarfs` | `{}` | Non-secret native settings by target alias: `instance_id`, `region_id`, `endpoint`, optional `volume_root`. |
| `keep_scratch` | `False` | Retain generated source and destination trees for inspection. |

`tasks` is the only concurrency control; there is no in-run sweep of `files`,
`file_size_bytes`, `tasks` or copiers: a run uses one copier per target. A run with
`tasks=16` starts 16 identical requests, dealt round-robin over the participating
machines; tasks above a machine's CPU count share its CPUs.

The `cp-r` copier is always GNU `cp -r --reflink=never`; it cannot be replaced by a
shell template. To compare native clone with ordinary materializing copies on
LakeBase, select `polarfs` for that target:

```python
CONFIG["copier"] = "cp-r"
CONFIG["target_copiers"] = {"LakeBase": "polarfs"}
CONFIG["polarfs"] = {
    "LakeBase": {
        "instance_id": "pfs-...",
        "region_id": "cn-hangzhou",
        "endpoint": "polardb.cn-hangzhou.aliyuncs.com",
        "volume_root": "/",
    },
}
```

All other targets use `cp-r`. Run once with this override and once without it,
then pool the two runs with `plot --vary target_copiers`; the `target/copier`
series keeps `LakeBase/polarfs` and `LakeBase/cp-r` apart. Use exact registered
target names, or the directory basenames when using `--dirs`. Every
`target_copiers` key is checked case-sensitively against the complete target
catalog (or all `--dirs` basenames) before a run is created. Unknown copier
names and malformed `polarfs` settings are rejected before tasks start, never an
implicit switch to `cp-r`. The alias check precedes `--target` filtering,
so valid overrides for unselected targets are allowed.

### Native PolarDB clone

The `polarfs` copier calls the official Alibaba Cloud Python SDK package
`alibabacloud_polardb20170801` 9.4.0, method
`clone_polar_fs_basic_snapshot`, with `PolarFsInstanceId`, `SourcePath`, and
`TargetPath`. Region and endpoint are per target. `SourcePath` and `TargetPath`
are volume-absolute, not host mount paths.

`volume_root` is the explicit volume path exposed at the target alias's mount
root. A full-volume mount uses `"/"` (the default). A mount created with
`--subdir /agents/agent-x` uses `"/agents/agent-x"`. The benchmark maps each host
source and destination under the mount by replacing that prefix; a path outside
the configured mount is rejected. It does not infer volume identity from the
mount type, device, or a guessed path mapping. The source directory must exist,
and the target's parent must exist while the target itself must not.

Credentials come only from the standard environment, never from `CONFIG`,
`config.json`, logs, or command arguments:

```sh
export ALIBABA_CLOUD_ACCESS_KEY_ID=...
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
# Optional, only for STS temporary credentials:
export ALIBABA_CLOUD_SECURITY_TOKEN=...
```

Set them in the environment of the benchmark process on every machine that may
run a native task; MPI and SSH do not forward the controller's environment.

The SDK call is synchronous: successful return is both the operation's end and
the experiment's persistence boundary, so no destination walk or `fsync` is
performed or timed. The SDK client is configured with retries disabled; the
benchmark makes no retry and has no `cp` fallback, so an SDK or HTTP error aborts
the run instead of emitting a latency row. Local path,
settings, credential-presence, and destination-state checks run before the timer.
See the
[ClonePolarFsBasicSnapshot API reference](https://help.aliyun.com/zh/polardb/api-polardb-2017-08-01-clonepolarfsbasicsnapshot)
and the
[directory clone interface](https://help.aliyun.com/zh/polardb/polardb-for-mysql/polardb-agent-lakebase-directory-clone).

Native clone does not request archive attributes. The experiment requires equal
file contents, directory structure, and independent subsequent writes, not
ownership/timestamps/ACL/xattr/hard-link preservation. Keep the SDK version,
endpoint, mount, and `volume_root` fixed and inspect them outside measurement when
interpreting results.

The baseline `cp-r` requires GNU coreutils; a non-GNU `cp` or one without
`--reflink` is rejected before tasks start. `--reflink=never` keeps the baseline a
materialized byte copy, followed by a successful recursive destination fsync and a
successful destination-parent fsync.

### Small smoke, not a performance experiment

`--smoke` overrides scale and repeats, while retaining configured copier
selection, layout, `tasks` and retention:

- 32 files of 4096 B, one empty directory, and two repeats.
- `batches.csv` gets one row per repeat; `requests.csv` gets `tasks`
  rows per batch.

At most 128 KiB of source payload is present per node in a smoke case. The
conservative free-space check includes a 256 MiB margin. No formal-size
benchmark is needed to check correctness or plotting.

## Workload and timing protocol

Every machine generates its own source tree; no directory is shared between
machines and no machine reads data prepared on another machine.

1. **Per node, on the leader task** (outside timing), after every task directory
   exists: resolve the target's copier on that machine, check its configuration,
   credentials, and explicit host-to-volume path mapping, then validate it once in
   a tiny scratch fixture (full content, empty directories, bidirectional
   source/destination write independence). Native settings and credentials are
   checked on every machine; the SDK client is created per request and is never
   broadcast. Check free space for the source and, for `cp-r`, every destination
   copy; generate and sync one source tree in `t.node_dir` from
   `random.Random(config.seed)` (byte-identical for the same seed on every machine
   and run); fsync the node directory and every ancestor up to the target root;
   record the filesystem type and either the `cp` executable SHA-256 digest or the
   native SDK version in `targets[].info.hosts[<node>]`; then wait at the node
   barrier.
2. **Per batch** (outside timing): every task removes its previous destination
   and creates a fresh destination parent in its own `t.task_dir`; every task
   prereads its node's source tree.
3. `t.batch(...)` releases all tasks together. Each task runs either GNU
   `cp -r --reflink=never` followed by destination files/directories synced
   bottom-up and destination-parent sync, or one synchronous native SDK call.
   The destination itself must initially be absent. Batch wall time is measured
   between the two MPI barriers, so thread/task startup and shutdown,
   verification, emission and cleanup are outside time.
4. Every task verifies its own copied tree against the node's source inventory
   (structure and sizes; contents were checked during validation).

There is no `threads` parameter: one task runs one copy operation (one external
`cp` process or one SDK call), and `tasks` counts independent operations. This
measures simultaneous template clones, not internal parallelization of a single
operation.

For `cp-r`, a successful `fsync` of the destination tree and parent is the
persistence contract, including on NFS/FUSE implementations. For `polarfs`, a
successful synchronous SDK return is the persistence contract; no `fsync` is
issued and `fsync_s` is fixed at `0`. No crash-recovery or remote failure
guarantee is claimed beyond those boundaries. Recursive sync includes directory
walking and file open/close overhead, so compare `cp-r`'s `latency_s` as a
**durable materializing-copy latency**, not pure metadata-clone API latency.

Every repeat prereads the source, but this neither pins data nor clears FUSE,
NAS or server caches. Call the state `source-preread-every-repeat`, not cold or
guaranteed fully warm. Use datasets that fit the intended warm-working-set
scenario; client memory and remote caches remain experimental conditions.

Unless `keep_scratch` is set, each node directory (source, scratch and
destinations) is removed after the run; only the empty run directory remains. A
failed run aborts the job and leaves the trees for inspection. Noisy copier
output goes to the job's stderr log; per node diagnostics live under
`results/clone/<run-id>/<target>/hosts/<node>/` in each machine's checkout.

## Results

The runner writes `results/clone/<run-id>/`:

- `config.json`: configuration and per-node facts under
  `targets[].info.hosts[<node>]`, including the filesystem type, copier name, and
  either the `cp` executable SHA-256 digest or the native SDK version.
- `batches.csv`: one row per target × repeat, for the run's one shape and copier.
- `requests.csv`: one row per task request within a batch.
- `figures/`: written only by `plot`, described below.

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
| `latency_s` | One request's copy-operation latency: `cp` command plus syncs, or the synchronous SDK call. |
| `copy_s`, `fsync_s` | One request's phases. For `cp-r`, `copy_s` is the `cp` process and `fsync_s` is destination-tree plus parent sync; for `polarfs`, `copy_s` is the SDK call and `fsync_s` is fixed at `0`. They sum to `latency_s`. |

Figures show the median with IQR error bars of the raw rows: a `batches` figure
takes a target and copier's repeats, a `requests` figure pools every request
sample across repeats. Use request-level rows for latency and batch-level rows
for wall time and throughput. Per-request latency starts
immediately before the `cp` process starts or immediately before the SDK call is
issued; batch time also includes barrier wake-up and the slowest task's
completion. Do not interpret MiB/s as physical bandwidth
for CoW clone; the primary metrics are latency and directories per second.

A `target/copier` pair identifies a series, e.g. `LakeBase/polarfs`,
`LakeBase/cp-r`, `NFS/cp-r`; operations cannot merge into one filesystem row.
The `plot` command pools several runs only when their source fingerprints and
`config.json` configurations match apart from `--vary`. Because one run holds one
shape, one copier per target and one `tasks` value, draw scaling curves and
copier comparisons from several runs and pass the varying keys explicitly.

## Default figures

`plot` renders the module's default `FIGURES` into `figures/` as vector PDF,
600 dpi PNG, and a standalone Python script with the raw samples. A figure is
split into one panel per distinct value of its `split` columns, so runs of
different shapes never share a line.

| Figure | Table | x | y | Split |
| --- | --- | --- | --- | --- |
| `latency-tasks` | `requests` | `tasks` | `latency_s` | `files`, `file_size_bytes`, log-log |
| `throughput-tasks` | `batches` | `tasks` | `clones_s` | `files`, `file_size_bytes` |
| `latency-bytes` | `requests` | `total_bytes` (MiB) | `latency_s` | `files`, `tasks`, log-log |
| `latency-files` | `requests` | `files` | `latency_s` | `file_size_bytes`, `tasks`, log-log |

Lines show medians with IQR error bars. Series are `target/copier`, ordered by
target. A figure holds at most 4 series; `--targets` selects and orders them by
target (`LakeBase`) or full series label (`LakeBase/polarfs`), putting the
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
