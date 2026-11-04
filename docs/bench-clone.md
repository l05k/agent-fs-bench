# Same-filesystem copy / clone benchmark

Measures tree copies from a source to a fresh destination on the same externally
prepared filesystem. Linux and Python 3.12+ are the supported runtime; the
benchmark never mounts or unmounts targets.

## Run

The public workload CLI exposes a single flag, `--smoke`:

```sh
# configured experiment (CONFIG defaults: stage single, copier cp, 3 repeats)
python -m agent_fs_bench run clone --dirs /mnt/fs-a

# real small smoke workload (not a mocked or alternate implementation)
python -m agent_fs_bench run clone --dirs /mnt/fs-a -- --smoke
```

Everything else is configured in the module-level `CONFIG` mapping at the top
of `agent_fs_bench/benchmarks/clone.py`; edit it to change an experiment before
running. `--smoke` replaces the selected stage with four small cases and keeps
the configured copiers, repeats, fsync, data pattern, and `kinds` filter:

- 8 MiB large file, C=1
- 200 × 8 KiB small files, C=1
- 16 MiB total across four 4 MiB files, one per worker, C=4
- 400 × 8 KiB small files, C=4

With the default one copier (`cp`) and three repeats this produces 12 JSONL
measurements and exercises large/small copies at C=1 and C=4, fsync, and
verification. Its conservative space check needs about 289 MiB free. Missing
copiers, insufficient space, copy/fsync failures, or failed verification stop
the run; cases are never silently skipped.

### CONFIG defaults

| Key | Default | Meaning |
| --- | --- | --- |
| `stage` | `"single"` | Workload stage: `single`, `concurrency`, or `all`. |
| `repeats` | `3` | Warm repetitions per case; must be positive. |
| `copiers` | `["cp"]` | Ordered copier names: `cp`, `cpr`, `internal`, or names declared in `external_copier`. |
| `external_copier` | `[]` | `NAME=COMMAND` entries adding external copiers (see below). |
| `threads` | `1` | Value substituted for `{threads}` in external commands. |
| `cp_binary` | `""` | Path to `cp`; empty resolves `cp` on `PATH`. |
| `fsync` | `True` | Fsync copied files and directories during the timed section. |
| `conc_levels` | `(1, 4, 16)` | Concurrency levels used by the `concurrency` stage. |
| `small_count` | `10000` | Small files per full-size small-file workload. |
| `data_pattern` | `"random"` | Source contents: `random` or `zeros`. |
| `kinds` | `()` | Empty keeps all kinds; otherwise a subset of `("large", "small")`. |
| `scratch_subpath` | `"scratch"` | Scratch directory relative to this run's work directory. |
| `keep_scratch` | `False` | Keep source and destination trees for inspection. |

`--smoke` is the only CLI flag; it replaces the stage selection with the four
small cases described above.

`CONFIG` is experiment setup, not an incidental switch: changing it changes the
measured payload or semantics, so keep it fixed across the filesystems you
compare.

## Workloads

- `single`: 256 MiB and 1 GiB large files; `small_count` (default 10,000) files
  at 4 KiB and 64 KiB. One copy worker.
- `concurrency`: a 2 GiB logical dataset and the `small_count`-file, 64 KiB
  dataset at each `CONFIG["conc_levels"]` value (default `1,4,16`). Large data
  is evenly sharded across workers.
- `all`: both stages, with duplicate specifications removed.
- `--smoke`: the four cases listed under Run.

Each case generates and fsyncs fresh source data beneath
`<target>/agent-fs-bench/clone/<run-id>/<scratch_subpath>` (default `scratch`),
then prereads the source once. Each copier/repeat gets a new destination.
Verification compares relative file names and byte sizes; file contents are
not hashed. Source and destination trees are removed afterward unless
`CONFIG["keep_scratch"]` is true. Scratch paths must remain inside the run
directory.

Random source data is the default. `CONFIG["data_pattern"] = "zeros"` is
explicit because compression, deduplication, and sparse-file detection can
make all-zero copies unrepresentative. `CONFIG["small_count"]` changes
full-size small-file workloads; `CONFIG["kinds"]` filters cases.

## Copiers

Default: `cp`. `CONFIG["copiers"]` is an ordered list, so duplicates and
repeated names are de-duplicated while keeping the first occurrence.

| Name | Implementation |
| --- | --- |
| `cp` | `cp -a` |
| `cpr` | `cp -r` |
| `internal` | 4 MiB chunked Python read/write with `copystat` |

`CONFIG["cp_binary"]` overrides the resolved executable. A generic external
copier can be supplied without adding CLI code:

```python
CONFIG["external_copier"] = [
    "reflink=cp -a --reflink=always {src} {dst}",
]
CONFIG["copiers"] = ["reflink"]
```

Templates use shell-like quoting but no shell execution. `{src}` and `{dst}`
are required; optional `{threads}` takes `CONFIG["threads"]` (default 1). Each
shard invokes the command once with an absent destination. Unsupported
operations fail rather than selecting another copier. Copier identity,
executable hashes, and full templates are retained in JSONL, including
external thread settings.

## Timing and cache semantics

- `copy_s`: copier invocation or Python copy loop, excluding fsync.
- `fsync_s`: destination file/directory fsync traversal, including its walk and
  open/close work. `CONFIG["fsync"]` defaults on; setting it to `False`
  measures page-cache completion instead.
- `wall_s`: the complete shard copy operation including fsync, per-shard
  destination existence/removal checks and parent-directory creation. C=1 runs
  directly; C>1 also includes `ProcessPoolExecutor` startup. The per-shard
  preparation precedes `copy_s`; directory creation performed by the copier
  itself is part of `copy_s`. Worker `copy_s` and `fsync_s` are **independent
  maxima**, not sums; they may come from different workers. Do not reconstruct
  `wall_s` by adding them.
- `mib_s` and `files_s` divide logical payload bytes/files by full `wall_s`.
- Source generation, preread, outer destination-root creation/removal before
  `copy_shards`, and verification are outside these timers. This does not
  exclude the per-shard setup described above from `wall_s`.

There is no cold mode: dropping kernel caches does not reset userspace client
caches. Every repeat declares `warm-source-preread`: one preread per case, no
explicit cache drop, no page-pinning guarantee. Datasets exceeding RAM may
become colder during later repeats.

`cp` may use reflink, `copy_file_range`, or server-side copying, while an
external metadata-only clone may move no payload at all. High logical MiB/s
therefore means tree-visible speed, not necessarily data movement. Filesystem
type is informational and may describe only a FUSE front end.

## JSONL results

The common runner appends one complete line per **case × copier × repeat** to
`results/clone/<run-id>/results.jsonl`. The envelope includes run, target,
environment, and effective `CONFIG` parameters (local executable paths are
replaced by per-copier SHA-256 hashes so grouping is machine-independent).
Numeric metrics include
`total_bytes`, `total_files`, `wall_s`, `copy_s`, `fsync_s`, `mib_s`, and
`files_s`. Details retain workload/kind, concurrency, copier/template identity,
fsync and cache semantics, data pattern, filesystem type, and successful
verification.

Use the common offline analysis/plot commands on exported JSONL; no live mount
or sidecar report is needed. The common runner derives `summary.csv` only after
every selected target succeeds. If a later target fails, earlier successful
measurements remain in JSONL, but no automatic CSV is produced. Compare only
matching workload, concurrency, copier/template, threads, fsync, cache state,
and data pattern. There is no workload-specific report command or CSV append option.
