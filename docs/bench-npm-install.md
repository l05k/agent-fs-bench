# npm-install FS benchmark

Measures how fast a prewarmed, offline npm/pnpm source materialises a tree into an **empty
`node_modules`** on the filesystem under test, at one or more externally concurrent batch sizes.
Linux and Python 3.12+ only; the benchmark never mounts or unmounts targets.

Only `node_modules` is empty — this is not a cold-cache measurement, because each timed install
reads a cache/store populated earlier on another local filesystem. The parent prereads each worker's
private source copy before the barrier, but residency is best effort and nothing verifies it.
Registry traffic is excluded from the timed section only: reads from the preparation filesystem and
traffic to the target (including network round trips for a remote target) stay inside the
measurement. Lifecycle scripts are off by default, so this is package materialisation, not
`postinstall` work. npm and pnpm install different graphs from the same fixture, so compare a
manager with itself. Tree metrics are an inventory, not bandwidth: no MB/s is reported and no claim
is made about physical write volume.

## Layout and results

Targets are mounted externally; only each worker's project is written to the target, while sources,
private homes and logs live elsewhere.

| Path | Purpose |
| --- | --- |
| `agent_fs_bench/benchmarks/npm_install.py` | stdlib workload module and `CONFIG`; `data/pnpm-bench/` holds the fixture inputs |
| `<target>/agent-fs-bench/npm-install/<run-id>/<manager>-c<C>-r<batch>/task-<i>/` | one worker's project |
| `<prep-dir>/agent-fs-bench/npm-install/<run-id>/<target>/` | `sources/`, `seeds/`, `templates/`, `batches/.../task-<i>/{source,home,tmp,xdg,config,data}` |
| `results/npm-install/<run-id>/{manifest.json,batches.csv,tasks.csv}` | run manifest, batch rows and per-worker samples; logs: `results/npm-install/<run-id>/<target>/logs/` |

Committed manifests and lockfiles are inputs, not result tables: a missing lockfile is an error,
and the seed aborts if a manager rewrote its lockfile. Preparation is per run, exclusive and
removed after success (`seed_only` keeps it, a failure leaves it for diagnosis); project trees are
removed after the inventory unless `keep_node_modules`, and the benchmark root stays on the target.

## Fixture

`CONFIG["fixture"]` names the tree pnpm's own install benchmark uses: `pnpm-bench`
(`pnpm11/benchmarks/fixture`, 84 deps + 4 devDeps). pnpm replays the committed `pnpm-lock.yaml`
with `--frozen-lockfile`; store and cache locations come only from the `--store-dir` and
`--cache-dir` arguments.

npm cannot read a pnpm lockfile and this tree has peer ranges its resolver rejects, so the fixture
carries a `package-lock.json` resolved with `--legacy-peer-deps`; seeding and timed installs both
run `npm ci` with that flag. Seeding is the only online phase, all timed installs are `--offline`,
and fixture selection is code-only.

## Configuration

The public workload CLI has no flags: run with the common `--dirs`/`--target` selection and edit
this mapping in `agent_fs_bench/benchmarks/npm_install.py`.

| Key | Default | Meaning |
| --- | --- | --- |
| `fixture` | `"pnpm-bench"` | Committed fixture tree. |
| `pm` | `"both"` | `npm`, `pnpm`, or `both`. |
| `runs` | `10` | Repeat batches per manager × C. |
| `concurrency` | `4` | Internal pool width per install. |
| `concurrent_jobs` | `[1]` | External batch sizes released together. |
| `prep_dir` | `"/dev/shm"` | External local filesystem for sources and copies. |
| `npm_bin` / `pnpm_bin` | `"npm"` / `"pnpm"` | Executables resolved on `PATH`. |
| `tool_versions` | node/npm/pnpm pins | Exact versions the run must observe: `v24.22.2`, `11.19.1`, `12.16.1`. |
| `ignore_scripts` | `True` | Pass `--ignore-scripts` everywhere. |
| `flush` | `True` | Timed whole-project fsync, pre-timing input fsync and between-batch `os.sync()`; `npm-install-nofsync` sets it `False`. |
| `keep_node_modules` / `seed_only` | `False` | Keep trees / prepare and exit. |

`tool_versions` is a strict check: the selected managers and `node` must report exactly these
versions or the run aborts before seeding; update the pins in the commit that changes the runtime.
`CONFIG` is experiment setup — changing it changes the measured payload, isolation or preparation
semantics — so keep it fixed across compared filesystems. Preparation is always external and local,
with no same-filesystem mode and no automatic fallback.

## Run

```sh
python -m agent_fs_bench run npm-install --dirs /mnt/fs-a
```

Default: `pnpm-bench`, both managers, ten batches each at one external install, pool 4. Typical changes:

```python
CONFIG["runs"] = 20                            # more batches
CONFIG["pm"] = "pnpm"                          # one manager
CONFIG["concurrent_jobs"] = [1, 2, 4, 8, 16]   # concurrency sweep
```

`CONFIG["runs"] = 1` is a minimal real smoke; it still needs registry access during seeding and a
valid preparation filesystem. Targets run sequentially within one invocation, in the given order:
rotate that order across independent invocations (each with a new run ID) so position does not
become a systematic advantage, and keep CPU count, CPU model, mount options and the preparation
filesystem fixed across comparisons; the manifest records `cpu_count` and the fstypes, not the
machine itself. `runs = 10` is the default, and more independent batches give a steadier median and
dispersion — often 10–20 per manager × C, from one run or pooled across runs. That range is a
convenience, not a minimum: nothing here computes a percentile, and batch-to-batch variation is part
of the result.

## Placement

`prep_dir` must exist (mount it yourself), and the run aborts unless its filesystem is a recognised
local type — `tmpfs`, `ext2`, `ext3`, `ext4`, `xfs`, `btrfs`, `zfs` — **and** both its `st_dev` and
its filesystem type differ from the target's. A different device alone does not exclude cross-mount
copy offload, since two mounts of the same local type can still serve a clone, so requiring both keeps
the timed write path from being satisfied by a server-side copy; there is no override or fallback.

`/dev/shm` (tmpfs) is the default; if the target is itself tmpfs the type rule rejects it, so use a
local disk filesystem. During a batch the preparation filesystem holds the seeded sources for every
selected manager, `C` copies of the current manager's source and each worker's `HOME`/`TMPDIR` —
check free space before a large `concurrent_jobs` entry, and expect concurrent runs on one root to
compete for its space and I/O.

Placement takes `Path` objects and resolves each filesystem type from Linux `/proc/self/mountinfo`
by the longest matching mount point. `tests/test_npm_install.py` passes real temporary directories,
so the device comparison runs against real `st_dev` values; the type lookup itself is stubbed there.

## Concurrency

| Knob | Default | Meaning |
| --- | --- | --- |
| `concurrency` | `4` | Pool width pinned inside each install; `0` keeps tool defaults. |
| `concurrent_jobs` | `[1]` | External batch sizes: installs released together, one sample each. |

`concurrent_jobs` is the experiment axis. Each value is its own `concurrent_jobs` value in
`batches.csv`; each batch emits one row, so a row never mixes batch sizes. Batches are synchronized
but independent: the parent prepares one project, one private source copy and one private
`HOME`/`TMPDIR`/XDG set per worker, fsyncs the inputs, prereads each worker's source, then releases
all workers through one barrier. Workers are separate Python processes, each timing its own child
install and flush; none shares a cache, store, home or project.

The pin reaches the pool each manager uses for filesystem work: npm gets `UV_THREADPOOL_SIZE`, pnpm
gets `RAYON_NUM_THREADS`. pnpm 12 is a Rust binary, and its link phase runs on the global Rayon pool
on Linux, so `RAYON_NUM_THREADS` sets the number of workers materializing package files. It is not a
manager-wide limit, and its effect is not comparable across managers: npm has no install-wide limit —
Arborist fans packages out to pacote — and its libuv pool is shared with work such as zlib. pnpm's
documented `child-concurrency`, `network-concurrency` and `workspace-concurrency` are not used here:
they bound lifecycle scripts, registry requests and workspace tasks, while the timed install is
`--ignore-scripts`, `--offline` and a single project.

## Workload and timing

1. **Seed (untimed, online).** Each manager's source is built once per run under `sources/<manager>`
   (npm cache; pnpm store plus XDG cache) from the committed inputs; the seed verifies the lockfile is
   unchanged and copies manifest plus lockfile into `templates/<manager>`. Nothing touches the target.
2. **Per-batch preparation (untimed).** Per worker: copy the whole source into
   `batches/<manager>-c<C>-r<batch>/task-<i>/source`, create the project from the template, create
   private `HOME`, `TMPDIR` and XDG directories, then **fsync the project tree and every new ancestor
   up to the target root**, so inputs are durable before timing. The parent then prereads each
   worker's private source copy once, and only then releases the barrier.
3. **Timed install.** All workers of one manager and one C are released by a barrier; each times its
   child from spawn to exit (`install_s`). npm runs `npm ci --offline --cache <source>/cache
   --legacy-peer-deps --no-audit --no-fund --no-update-notifier`; pnpm runs `pnpm install --offline
   --frozen-lockfile --store-dir <source>/store --cache-dir <source>/cache
   --package-import-method=copy --config.enable-global-virtual-store=false --reporter=append-only`.
4. **Timed flush.** The worker fsyncs the **whole project**, not just `node_modules`: every regular
   file, then every directory bottom-up so each is persisted after the entries it gained. Symlinks
   are not followed; their entries persist through the containing directory. Walk, `open`, `fsync`
   and `close` are all inside `flush_s`, so a high-latency filesystem pays for directory reads and
   per-file syscalls here; `durable_s = install_s + flush_s`.
5. **Inventory and cleanup (untimed).** Once every worker reaches durability, the parent walks each
   `node_modules` for tree metrics, then removes project trees (unless `keep_node_modules`) and source
   copies; a global `os.sync()` runs only between batches, never inside a timed section.

`install_s` is full wall time for the command — spawn, module loading, lockfile resolution,
extraction — plus reading the external source; it excludes preparation, preread, inventory and
cleanup. `durable_s` adds the whole-project flush. **The flush is the benchmark's protocol, not the
tools' native durability semantics:** both may leave data in the page cache, and this workload makes
the tree durable itself, so do not read `install_s` as what npm/pnpm guarantee or `durable_s` as
their implementation detail.

## No-fsync variant

`run npm-install-nofsync` runs the same module through
`agent_fs_bench/benchmarks/npm_install_nofsync.py`, which only sets
`CONFIG["flush"] = False`. It removes every harness-initiated sync: the timed
whole-project flush, the pre-timing input fsync, and all three `os.sync()`
calls — after seeding, before each batch, and after each batch. Step 2 keeps its
project-placement check but drops the fsync, step 4 does not exist, and step 5
drops its sync calls, so nothing in the benchmark forces the tree to stable
storage. The check that a project lies inside the measured target is not a sync
and stays unconditional.

`batches.csv` carries only `install_mean_s`, `batch_install_s`, `installs_per_s`, `user_s`,
`sys_s` and the tree inventory; `flush_*`, `durable_*` and `durable_installs_per_s` are absent,
and `tasks.csv` has no `flush_s` or `durable_s`. The manifest `config` records `flush` (`true` for
`npm-install`, `false` here), and results live under `results/npm-install-nofsync/`. `install_s`
still covers the whole command, including whatever the tool itself syncs; it ends at process exit,
so it describes page-cache completion, not durability. Without the between-batch `os.sync()`,
writeback from one batch may still be in flight when the next batch starts.

## pnpm import method

pnpm uses `--package-import-method=copy` with the global virtual store off; `hardlink` and `auto`
would link store contents into `node_modules` and measure the link path. With `copy`, the source is a
controlled copy on the preparation filesystem, so a cross-source clone or reflink cannot serve the
install. That prevents offload across sources but does not guarantee a physical write volume — a
filesystem may still deduplicate or compress blocks — so `allocated_bytes` and `du_kib` describe what
the tree occupies, not how much was written. npm always unpacks from its cache into new files.

## Tables

Two flat CSV tables per run; `run` also writes `summary/batches.csv` and `summary/tasks.csv` with
n and the median of each metric per target × manager × `concurrent_jobs` (both `repeat` and
`sample` are pooled).

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `batches` | `target`, `manager`, `concurrent_jobs`, `repeat` | `install_mean_s`, `flush_mean_s`, `durable_mean_s`, `batch_install_s`, `batch_durable_s`, `installs_per_s`, `durable_installs_per_s`, `user_s`, `sys_s`, `files`, `dirs`, `symlinks`, `hardlinks`, `logical_bytes`, `allocated_bytes`, `du_kib` |
| `tasks` | `target`, `manager`, `concurrent_jobs`, `repeat`, `sample` | `install_s`, `flush_s`, `durable_s`, `user_s`, `sys_s`, `start_offset_s` |

`repeat` is the 1-based batch index and `sample` the 1-based worker within the batch, so every
worker's raw, unrounded sample is one `tasks` row. `npm-install-nofsync` emits the same tables
without the flush and durable columns (`flush_mean_s`, `durable_mean_s`, `batch_durable_s`,
`durable_installs_per_s`, `flush_s`, `durable_s`).

| Metric | Meaning |
| --- | --- |
| `install_mean_s`, `flush_mean_s`, `durable_mean_s` | Mean `install_s`, `flush_s`, `durable_s` over the batch. |
| `batch_install_s` / `batch_durable_s` | Wall time to the last exit / durability point: `max(start_offset_s + install_s)` / `max(start_offset_s + durable_s)`. |
| `installs_per_s` / `durable_installs_per_s` | `C / batch_install_s` / `C / batch_durable_s`. |
| `user_s`, `sys_s` | Summed child CPU time. |
| `files`, `dirs`, `symlinks`, `hardlinks`, `logical_bytes`, `allocated_bytes`, `du_kib` | Summed tree inventory. |

`start_offset_s` is measured from the parent's last monotonic reading before the barrier releases
the workers, so `batch_*_s` includes release skew instead of assuming a simultaneous start. Means
describe one install, batch times describe the batch, and throughput is a batch quantity, never a
per-install latency.

Discovered facts live in `manifest.json`: `targets[].info` holds `target_fstype`, `prep_fstype`,
`cpu_count`, `tool_versions` per manager and `seed_input_sha256` of the committed template inputs;
`targets[].path` holds the target directory, `run_id` the run, and `config` the run settings
(`fixture`, `pm`, `runs`, `concurrency`, `concurrent_jobs`, `prep_dir`, `ignore_scripts`, `flush`,
`keep_node_modules`, `seed_only`, executable names, version pins). Device IDs and resolved
executable paths are not stored. `plot` pools repeated runs only when their configs and source
fingerprints match; a non-zero exit, missing tree, walk or fsync failure, or failed `du` aborts the
run and emits no row for that batch.

## Figures

`run` renders the default figures into `results/npm-install/<run-id>/figures/`, one file per
manager; `python -m agent_fs_bench plot <run-dir>...` re-renders them.

| Figure | Table | X | Y |
| --- | --- | --- | --- |
| `batches-install_mean_s-<manager>` | `batches` | `concurrent_jobs` | `install_mean_s` |
| `batches-durable_mean_s-<manager>` | `batches` | `concurrent_jobs` | `durable_mean_s` |

Each point is the median over repeats with IQR error bars, the x axis is logarithmic, and the series
is the target, so several targets of one run share a figure. `npm-install-nofsync` renders only the
install-time figure. A concurrency sweep uses `concurrent_jobs = [1, 2, 4, 8, 16]` with enough
`runs` per value; latency and throughput stay in separate figures because one is per install and the
other per batch. A one-off figure comes from the same tables:
`python -m agent_fs_bench plot results/npm-install/<run-id> --table batches --x concurrent_jobs
--y installs_per_s --split manager --ylabel 'Installs/s'`. This repository commits no generated
plots and never invents data to fill one.

## Notes

The timed install reads the preparation filesystem every run, so keep it local and quiet. `install_s`
includes process startup, so increase `runs` rather than shrinking the workload. The benchmark root
stays on the target; delete it yourself. Changing the timed command, flush protocol or isolation changes
`source_sha256`; select one source identity rather than pooling old and new measurements.
