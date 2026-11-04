# Same-filesystem copy / clone benchmark

Measures how fast one filesystem can copy a tree from a source to a new
destination on the same mount. The benchmark covers large-file throughput,
small-file throughput, and process-level copy concurrency.

测量同一文件系统内的树复制/克隆速度，覆盖大文件、小文件和并发复制三个维度。

## Workloads

- `single`: 256 MiB and 1 GiB large files, plus 10,000 files at 4 KiB and
  64 KiB. One copy worker.
- `concurrency`: a 2 GiB logical dataset and the 10,000-file dataset at each
  level of `--conc-levels` (default `1,4,16`). Large data is split evenly
  across one shard per worker.
- `--smoke`: four small cases for a quick end-to-end check.

For every case the harness:

1. generates a fresh source tree under `--scratch-subpath` and fsyncs it;
2. reads the source once to warm the page cache;
3. removes the destination and copies into a new tree for each repeat;
4. verifies the destination's relative file set and file sizes;
5. removes the source and destination unless `--keep-scratch` is set.

The repeat number is recorded, but every repeat has the same declared cache
state. All repeats are aggregated with a median.

Source data is random by default (`--data-pattern random`). On filesystems
with compression, deduplication, or automatic sparse-file detection, all-zero
data can make a copy look much faster than it would for real payloads. Use
`--data-pattern zeros` only for quick smoke runs or for reproducing the old
prototype, and record the pattern when comparing results.

## Run / 运行

```sh
uv run clone_bench.py --mount /mnt/testfs --copiers cp,cpr,internal
uv run clone_bench.py --mount /mnt/testfs --stage concurrency \
  --copiers cp,internal --conc-levels 1,4,16,32
uv run clone_bench.py --mount /mnt/testfs --smoke --repeats 2
uv run clone_bench.py report --raw results/clone_bench/<run-id>/raw.csv
```

`--copier` is repeatable and `--copiers` accepts the same comma-separated
names. The built-ins are:

| name | implementation |
| --- | --- |
| `cp` | `cp -a` |
| `cpr` | `cp -r` |
| `internal` | 4 MiB chunked Python read/write, with `copystat` |

`--fsync` is on by default. It fsyncs every copied file and directory after
the copy phase and reports that time separately. Use `--no-fsync` to measure
only page-cache completion.

### External command copiers

A dedicated client no longer needs to be compiled into the benchmark. Define
it as a command template:

```sh
uv run clone_bench.py --mount /mnt/lakebase \
  --external-copier \
    'lbclone=lakebase-client clone -p --threads {threads} {src} {dst}' \
  --copier lbclone --stage single --threads 8
```

The value is parsed with shell-like quoting, not through a shell. It must
contain `{src}` and `{dst}`; `{threads}` is replaced from `--threads`. The
command is invoked once per shard, with the destination absent. After it
succeeds, the harness fsyncs the destination unless `--no-fsync` was passed.

This keeps client-specific flags and paths in the operator's command line
rather than in the benchmark. The same mechanism works for `pfs clone`,
`cp --reflink=always`, a vendor sync tool, or a wrapper script.

## Measurement semantics

- `wall_s` is the complete timed copy operation. It includes `copy_s` and
  `fsync_s`.
- `copy_s` is the copier invocation or Python copy loop only. It excludes the
  fsync phase.
- For C=1, the copier runs in the harness process (the `cp` built-in still
  starts a child process, as it should). For C>1, `wall_s` also includes
  `ProcessPoolExecutor` startup; `copy_s` does not.
- Large cases report logical payload MiB/s; small cases report files/s.
  Both are `total_bytes` or `total_files` divided by `wall_s`.
- A metadata-only clone is a valid external copier, but its high logical
  MiB/s means "tree visible" speed, not data movement. The report labels the
  copier name and does not imply that it moved the payload.

### Warm cache, not "cold"

There is no cold mode. `drop_caches` cannot evict a FUSE or other userspace
client's cache, so a run after dropping the kernel cache can still be warm at
the layer that dominates the measurement. Calling that "cold" would produce
misleading comparisons. Instead, the source is pre-read once, the state is
recorded as `warm-source-preread`, and the report never mixes cache states.

Warm-up is best effort. The kernel may evict pages during a long case, and
userspace client caches are not reset between repeats. The declared state
therefore means "no explicit cache drop; warm-up attempted", not "pinned".

### What is verified

After each timed copy, the destination's relative path set and byte sizes are
compared with the source. A missing file, extra file, or size mismatch fails
the run. File contents are not hashed, because hashing multi-gigabyte trees
would dominate the benchmark.

## Design corrections

The imported prototype had several experiment-design problems that are fixed
here:

- Removed the fake cold mode and the global `drop_caches` call. Repeats are
  all explicitly warm and are aggregated together.
- Removed hardcoded mount names, `/root` result paths, the
  `/root/lakebase-client-latest` default, and the fixed mount display labels.
- Replaced the special `lbclone` branch with generic `--external-copier`
  command templates.
- Fixed the single-shard path going through `ProcessPoolExecutor` and charging
  pool startup to wall time.
- Replaced all-zero payloads with random data by default, avoiding compression,
  deduplication, and zero-hole detection from inflating throughput.
- Measured `internal` fsync separately instead of hiding it inside `copy_s`.
- Grouped reports by explicit `workload`, `kind`, `concurrency`, `copier`,
  `fsync`, `cache_state`, and `data_pattern` fields instead of guessing
  concurrency from a `_C` substring in the tag.
- Stopped merging rows from different cache states in the same aggregate.
- Added destination verification after every repeat instead of checking only
  file counts for one clone copier.
- Added failure cleanup, insufficient-space checks, and schema validation
  before appending to an existing CSV.
- Used `/proc/self/mountinfo` before falling back to `findmnt` or `stat`, so
  ext4 on minimal Linux images is not reported as `ext2/ext3`.
- Removed the hardcoded `[1,4,16,32,64,128]` report levels; reports use the
  levels actually present in `raw.csv`.

## Remaining limitations

- `cp` may use `copy_file_range`, reflink, or a server-side copy depending on
  the kernel and filesystem. That is real `cp` behaviour, but it means `cp`
  and `internal` are not guaranteed to move the same amount of data.
- Warm-up does not pin pages. For datasets larger than available memory, later
  repeats can be colder than the first.
- Verification checks names and sizes, not contents.
- Process-worker startup is included in multi-shard wall time because the
  benchmark measures the user-visible operation, not only the copy syscalls.
- Filesystem type is informational. A FUSE mount may be reported as `fuse`
  while the backing filesystem has different characteristics.

## Artifacts

Every run writes to
`results/clone_bench/<UTC-run-id>/` next to the script:

| file | contents |
| --- | --- |
| `raw.csv` | one row per repeat and copier |
| `summary.md` | detail, copier comparison, mount comparison, concurrency curves |
| `result.json` | structured metadata and all rows |
| `manifest.txt` | argv, host, Python, mount, fstype, copier templates, cache/fsync mode |

`--append-csv PATH` additionally appends the same rows to a shared CSV. The
header is created when the file is new, and an existing file with a different
schema is rejected rather than silently mixing formats.

CSV columns:

```text
ts run_id mount fstype workload kind concurrency threads total_bytes
total_files data_pattern copier fsync cache_state repeat wall_s copy_s fsync_s
mib_s files_s verified
```
