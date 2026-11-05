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
registered target names, or the directory basenames when using `--dirs`.
Before creating a run, every `target_copiers` key is checked against the complete
registered target catalog (or all `--dirs` basenames), case-sensitively. An
unknown key is an error, never an implicit switch to `cp-r`. This check precedes
`--target` filtering: valid overrides for unselected targets are allowed, and
only selected copiers need installed executables.

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
- Two repeats: eight cases and 16 JSONL rows per copier, 28 actual requests.

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

## Results and interpretation

The common runner writes `results/clone/<run-id>/results.jsonl` and derives
`summary.csv` after all targets succeed. One row is one workload × copier ×
repeat. The raw `target` still identifies the filesystem; `details.copier`
identifies the operation. `case` is workload-only:

```text
<bytes|files|concurrency>/N<files>_S<file-size-bytes>/C<requests>
```

Metrics:

| Metric | Meaning |
| --- | --- |
| `total_bytes`, `total_files` | Payload of **one** complete directory request. |
| `concurrency` | Requests in this batch. |
| `wall_s` | Barrier release through last completion; excludes worker creation/shutdown. |
| `clones_s` | Completed directories / batch wall time. Finite-batch throughput, not a steady-state offered-load test. |
| `latency_s` | Median request command-plus-sync latency in this batch. |
| `copy_s`, `fsync_s` | Independent medians of request command and sync phases; do not add these medians to reconstruct batch time. |

`details.requests` retains every request's unrounded `copy_s`, `fsync_s` and
`latency_s`. Each individual request's phases sum to its latency. Per-request
latency starts immediately before command execution; batch time also includes
barrier wake-up/scheduling. `details` also records experiment, workload kind,
copier and filesystem type. `parameters` records resolved command templates,
executable SHA-256 digests, layout/scales, repetitions, cache and completion
contract. Source hashes identify the checkout but do not replace archiving it.

Summary series are `target/copier`, e.g. `LakeBase/lakebase`, `LakeBase/cp-r`,
`NFS/cp-r`; operations cannot merge into one filesystem row. Summary latency
statistics summarize **per-batch medians**. Paper phase/latency figures instead
pool original request samples. Do not interpret MiB/s as physical bandwidth
for CoW clone; the primary metrics are latency and directories per second.

The shared summary still merges selected cross-run samples without separating
arbitrary configuration or host changes. Keep input selections comparable.
This is a clean cutover from the old sharded format: remeasure old clone runs;
do not combine their case names or statistics with new results.

## Paper figures

The existing plot CLI automatically generates numeric-x line figures for
clone; no extra plot flags. The selected metric produces only groups present
in its input: `<stem>-bytes`, `<stem>-files`, `<stem>-concurrency-large`,
`<stem>-concurrency-small`, each as vector PDF, 600 dpi PNG and standalone Python
with the raw samples. Numeric x points are sorted; `--cases` filters workloads
rather than rearranging a numeric sweep. `--targets` selects/orders exact
`target/copier` series; put the paper's primary system first.

```sh
uv run --group analysis python -m agent_fs_bench plot \
  'results/clone/<run-id>/results.jsonl' \
  --benchmark clone --metric latency_s \
  --targets LakeBase/lakebase,LakeBase/cp-r,NFS/cp-r \
  --output results/clone-latency

uv run --group analysis python -m agent_fs_bench plot \
  'results/clone/<run-id>/results.jsonl' \
  --benchmark clone --metric clones_s \
  --targets LakeBase/lakebase,LakeBase/cp-r,NFS/cp-r \
  --output results/clone-throughput
```

Recommended main comparisons: byte/file sensitivity using `latency_s`, and
large/small concurrency using `clones_s` together with `latency_s`. Lines show
medians and IQR error bars. Phase metrics pool `details.requests`; throughput
uses one batch value per repeat. Size/count axes are logarithmic; positive
latency/phase axes are logarithmic. Selected series must cover identical x
values; incompatible fixed payloads in a sweep fail instead of plotting a
misleading comparison. Arbitrary mount/cache/provenance differences are not
automatically validated. Tiny smoke samples do not support tail-latency claims.

Style reuses the project Arial 9pt palette, filled markers, boxed axes, top
legend and no internal grids. Default single-column size is 3.5 × 1.5 inches;
use the existing `--size` only if a specific paper layout requires it. Standalone
scripts regenerate PDF/PNG without JSONL or the installed benchmark package.
