# npm-install FS benchmark

Measures how fast a prewarmed, offline npm/pnpm source materialises a tree into an **empty
`node_modules`** on the filesystem under test, with `tasks` independent installs running at the
same time. Linux and Python 3.12+ only; the benchmark never mounts or unmounts targets.

Only `node_modules` is empty — this is not a cold-cache measurement, because every timed install
reads a cache/store populated earlier on another local filesystem. Each machine seeds its own
source and every task copies that source before the barrier, but residency is best effort and
nothing verifies it. Registry traffic is excluded from the timed section only: reads from the
preparation filesystem and traffic to the target (including network round trips for a remote
target) stay inside the measurement. Lifecycle scripts are off by default, so this is package
materialisation, not `postinstall` work. npm and pnpm install different graphs from the same
fixture, so compare a manager with itself. Tree metrics are an inventory, not bandwidth: no MB/s is
reported and no claim is made about physical write volume.

## Run and configure

```sh
# Controller only
uv run python -m agent_fs_bench run npm-install --mode local --dirs /mnt/fs-a
# Controller first, then the local/hosts.toml machines in file order
uv run python -m agent_fs_bench run npm-install --mode overflow --dirs /mnt/fs-a
# Only the local/hosts.toml machines; the controller launches mpiexec and hosts no task
uv run python -m agent_fs_bench run npm-install --mode remote --dirs /mnt/fs-a
```

`tasks` is one integer per run: the number of simultaneous independent installs. Every other
experiment dimension is one value per run too — one manager, one fixture — and `repeats` repeats
that one configuration. A curve over `tasks` comes from several runs pooled when plotting, e.g.
`plot results/npm-install/<run-a> results/npm-install/<run-b> --vary tasks`.

The controller allocates `tasks` slots across the machines of the mode and launches **one** global
Open MPI job: one task process per slot, each pinned to its machine and CPU set. Every machine
needs the same checkout with its own `uv sync --locked` environment, a working Open MPI 4.1 and
`mpi4py`, the selected package manager, `node`, and a `prep_dir` on a local filesystem. Seeding is
online, so **every machine that hosts a task needs registry access** during the untimed preparation
step.

The public workload CLI has no flags: run with the common `--mode`/`--dirs`/`--target` selection and
edit this mapping in `agent_fs_bench/benchmarks/npm_install.py`.

| Key | Default | Meaning |
| --- | --- | --- |
| `fixture` | `"pnpm-bench"` | Committed fixture tree. |
| `pm` | `"npm"` | One manager per run: `npm` or `pnpm`. |
| `repeats` | `10` | Independent barrier-aligned batches of the same configuration. |
| `tasks` | `1` | Simultaneous independent installs, placed by the controller. |
| `cpus_per_task` | `4` | Logical CPUs pinned per install; keep it equal to `concurrency`. |
| `oversubscribe` | `1` | Tasks allowed per CPU group, i.e. per `cpus_per_task` CPUs. |
| `concurrency` | `4` | Internal pool width per install; `0` keeps tool defaults. |
| `prep_dir` | `"/dev/shm"` | External local filesystem for sources and copies, on every machine. |
| `npm_bin` / `pnpm_bin` | `"npm"` / `"pnpm"` | Executables resolved on each machine's `PATH`. |
| `tool_versions` | node/npm/pnpm pins | Exact versions every machine must observe: `v24.22.2`, `11.19.1`, `12.16.1`. |
| `ignore_scripts` | `True` | Pass `--ignore-scripts` everywhere. |
| `flush` | `True` | Timed whole-project fsync, pre-timing input fsync and the leader's `os.sync()`; `npm-install-nofsync` sets it `False`. |
| `keep_node_modules` / `seed_only` | `False` | Keep trees / seed every machine and exit. |

`tool_versions` is a strict check on each machine: the selected manager and `node` must report
exactly these versions or the run aborts before seeding; update the pins in the commit that changes
the runtime. `CONFIG` is experiment setup — changing it changes the measured payload, isolation or
preparation semantics — so keep it fixed across compared filesystems. Preparation is always
external and local, with no same-filesystem mode and no automatic fallback.

`tasks` must fit the pool: `tasks × cpus_per_task` may not exceed the CPUs of the machines the mode
offers, times `oversubscribe`; a request above capacity is an error, never a smaller run. A
`repeats = 1`, `tasks = 1` run in `--mode local` is a minimal real smoke; it still needs registry
access during seeding and a valid preparation filesystem.

## Layout and results

Targets are mounted externally. Only each task's project is written to the target; sources, private
homes and logs live elsewhere. Nothing is shared between machines: every path below the preparation
root is per machine.

| Path | Purpose |
| --- | --- |
| `agent_fs_bench/benchmarks/npm_install.py` | stdlib workload module, `CONFIG`, `run` and `task`; `data/pnpm-bench/` holds the fixture inputs |
| `<target>/agent-fs-bench/npm-install/<run-id>/<node>/task-NNNN/repeat-NNNN/` | one task's project for one repeat, on its own machine |
| `<prep-dir>/agent-fs-bench/npm-install/<run-id>/<target>/<node>/` | that machine's `source/`, `template/`, `seeds/` and `repeats/repeat-NNNN/task-NNNN/{source,home,tmp,xdg,config,data}` |
| `results/npm-install/<run-id>/{manifest.json,batches.csv,installs.csv}` | run manifest, batch rows and per-install samples |
| `results/npm-install/<run-id>/<target>/hosts/<node>/` | that machine's diagnostics: per-repeat install logs and the seed log |
| `results/npm-install/<run-id>/<target>/tasks.stderr.txt` | the MPI job's stderr, including every task's child output |

Committed manifests and lockfiles are inputs, not result tables: a missing lockfile is an error,
and the seed aborts if a manager rewrote its lockfile. Each machine's preparation tree is exclusive
to the run and removed after its last repeat (`seed_only` keeps it, a failure leaves it for
diagnosis); project trees are removed after the inventory unless `keep_node_modules`, and the
benchmark root stays on the target.

## Fixture

`CONFIG["fixture"]` names the tree pnpm's own install benchmark uses: `pnpm-bench`
(`pnpm11/benchmarks/fixture`, 84 deps + 4 devDeps). pnpm replays the committed `pnpm-lock.yaml`
with `--frozen-lockfile`; store and cache locations come only from the `--store-dir` and
`--cache-dir` arguments.

npm cannot read a pnpm lockfile and this tree has peer ranges its resolver rejects, so the fixture
carries a `package-lock.json` resolved with `--legacy-peer-deps`; seeding and timed installs both
run `npm ci` with that flag. Seeding is the only online phase, all timed installs are `--offline`,
and fixture selection is code-only.

## Placement

`prep_dir` must exist on every machine that hosts a task (mount it yourself). On each machine the
node leader compares that machine's `prep_dir` with that machine's target and aborts unless its
filesystem is a recognised local type — `tmpfs`, `ext2`, `ext3`, `ext4`, `xfs`, `btrfs`, `zfs` —
**and** both its `st_dev` and its filesystem type differ from the target's. A different device alone
does not exclude cross-mount copy offload, since two mounts of the same local type can still serve
a clone, so requiring both keeps the timed write path from being satisfied by a server-side copy;
there is no override or fallback.

`/dev/shm` (tmpfs) is the default; if the target is itself tmpfs the type rule rejects it, so use a
local disk filesystem. During a repeat each machine's preparation filesystem holds the seeded source
plus one copy per task on that machine, and each task's `HOME`/`TMPDIR` — check free space before a
large `tasks`, and expect concurrent runs on one root to compete for its space and I/O. The rule is
evaluated per machine, so a pool whose machines mount different preparation filesystems is accepted;
the manifest records each machine's `prep_fstype` and `target_fstype`.

Placement takes `Path` objects and resolves each filesystem type from Linux `/proc/self/mountinfo`
by the longest matching mount point. `tests/test_npm_install.py` passes real temporary directories,
so the device comparison runs against real `st_dev` values; the type lookup itself is stubbed there.

## Concurrency

| Knob | Default | Meaning |
| --- | --- | --- |
| `tasks` | `1` | External concurrency: installs released together, one sample each. |
| `cpus_per_task` | `4` | CPUs pinned to each task process. |
| `oversubscribe` | `1` | Tasks per CPU group. |
| `concurrency` | `4` | Pool width pinned inside each install; `0` keeps tool defaults. |

`tasks` is the experiment axis, and it is one value per run. The controller fills machines in order
and uses every CPU group of a machine once before reusing one, so `tasks = 4` with
`cpus_per_task = 4` needs 16 logical CPUs; `oversubscribe = 2` accepts two tasks per group on a
machine that is short of CPUs, at the cost of two installs sharing a pinned set.

Keep `cpus_per_task` equal to `concurrency`. The pin only helps if the pool it names has the CPUs
to run on: with a wider pool than the task's CPU set the extra workers time-share the same CPUs,
and with a narrower pool the pinned CPUs idle. `cpus_per_task` is enforced by the launcher's
affinity mask, `concurrency` by the environment; nothing reconciles them automatically.

The pin reaches the pool each manager uses for filesystem work: npm gets `UV_THREADPOOL_SIZE`, pnpm
gets `RAYON_NUM_THREADS`. pnpm 12 is a Rust binary, and its link phase runs on the global Rayon pool
on Linux, so `RAYON_NUM_THREADS` sets the number of workers materializing package files. It is not a
manager-wide limit, and its effect is not comparable across managers: npm has no install-wide limit —
Arborist fans packages out to pacote — and its libuv pool is shared with work such as zlib. pnpm's
documented `child-concurrency`, `network-concurrency` and `workspace-concurrency` are not used here:
they bound lifecycle scripts, registry requests and workspace tasks, while the timed install is
`--ignore-scripts`, `--offline` and a single project.

## Workload and timing

1. **Node preparation (untimed, online).** On each machine's node leader: the Linux check, the
   `prep_dir` existence and placement checks, the tool version check, then the seed under that
   machine's `source/` (npm cache; pnpm store plus XDG cache) from the committed inputs. The seed
   verifies the lockfile is unchanged, copies manifest plus lockfile into `template/`, and the leader
   records `target_fstype`, `prep_fstype`, `tool_versions` and `seed_input_sha256` as
   this node's manifest facts. Nothing touches the target. With `seed_only` every task stops here
   and the sources stay on their machines.
2. **Per-repeat preparation (untimed).** Per task: copy the machine's whole source into
   `repeats/repeat-NNNN/task-NNNN/source`, create the project from the template under the task's own
   directory, create private `HOME`, `TMPDIR` and XDG directories, then — with `flush` — **fsync the
   project tree and every new ancestor up to the target root**, so inputs are durable before timing.
   The node then reaches a node barrier, the node leader runs one `os.sync()` (`flush` only), and
   each task prereads its own source copy once.
3. **Timed install.** `t.batch` releases every task of the whole job through one world barrier; each
   task times its child from spawn to exit (`install_s`). npm runs
   `npm ci --offline --cache <source>/cache --legacy-peer-deps --no-audit --no-fund
   --no-update-notifier`; pnpm runs `pnpm install --offline --frozen-lockfile --store-dir
   <source>/store --cache-dir <source>/cache --package-import-method=copy
   --config.enable-global-virtual-store=false --reporter=append-only`.
4. **Timed flush.** With `flush`, each task fsyncs its **whole project**, not just `node_modules`:
   every regular file, then every directory bottom-up so each is persisted after the entries it
   gained. Symlinks are not followed; their entries persist through the containing directory. Walk,
   `open`, `fsync` and `close` are all inside `flush_s`, so a high-latency filesystem pays for
   directory reads and per-file syscalls here; `durable_s = install_s + flush_s`. The batch ends at
   the world barrier after every task reached durability.
5. **Inventory and cleanup (untimed).** Each task walks its own `node_modules` for tree metrics, task
   0 sums them; project trees are removed (unless `keep_node_modules`) together with the task's
   sandbox. After the last repeat each machine's leader removes its preparation directory. No source
   write, inventory or deletion overlaps a timed install.

`install_s` is full wall time for the command — spawn, module loading, lockfile resolution,
extraction — plus reading the external source; it excludes preparation, preread, inventory and
cleanup. `durable_s` adds the whole-project flush. **The flush is the benchmark's protocol, not the
tools' native durability semantics:** both may leave data in the page cache, and this workload makes
the tree durable itself, so do not read `install_s` as what npm/pnpm guarantee or `durable_s` as
their implementation detail.

## No-fsync variant

`run npm-install-nofsync` runs the same module through
`agent_fs_bench/benchmarks/npm_install_nofsync.py`, which only sets `CONFIG["flush"] = False`. It
removes every harness-initiated sync: the timed whole-project flush, the pre-timing input fsync, and
the node leader's `os.sync()`. Step 2 keeps its project-placement check but drops the fsync, step 4
does not exist, and nothing in the benchmark forces the tree to stable storage. The check that a
project lies inside the measured target is not a sync and stays unconditional.

`batches.csv` carries only `install_mean_s`, `batch_install_s`, `installs_per_s`, `user_s`, `sys_s`
and the tree inventory; `flush_*`, `durable_*` and `durable_installs_per_s` are absent, and
`installs.csv` has no `flush_s` or `durable_s`. The manifest `config` records `flush` (`true` for
`npm-install`, `false` here), and results live under `results/npm-install-nofsync/`. `install_s`
still covers the whole command, including whatever the tool itself syncs; it ends at process exit,
so it describes page-cache completion, not durability. Without the leader's `os.sync()`, writeback
from one repeat may still be in flight when the next repeat starts.

## pnpm import method

pnpm uses `--package-import-method=copy` with the global virtual store off; `hardlink` and `auto`
would link store contents into `node_modules` and measure the link path. With `copy`, the source is a
controlled copy on the preparation filesystem, so a cross-source clone or reflink cannot serve the
install. That prevents offload across sources but does not guarantee a physical write volume — a
filesystem may still deduplicate or compress blocks — so `allocated_bytes` and `du_kib` describe what
the tree occupies, not how much was written. npm always unpacks from its cache into new files.

## Tables

Two flat CSV tables per run; `run` also writes `summary/batches.csv` and `summary/installs.csv` with
n and the median of each metric per target × manager × `tasks` (both `repeat` and `sample` are
pooled).

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `batches` | `target`, `manager`, `tasks`, `repeat` | `install_mean_s`, `flush_mean_s`, `durable_mean_s`, `batch_install_s`, `batch_durable_s`, `installs_per_s`, `durable_installs_per_s`, `user_s`, `sys_s`, `files`, `dirs`, `symlinks`, `hardlinks`, `logical_bytes`, `allocated_bytes`, `du_kib` |
| `installs` | `target`, `manager`, `tasks`, `repeat`, `sample`, `host` | `install_s`, `flush_s`, `durable_s`, `user_s`, `sys_s` |

`repeat` is the 1-based batch index, `sample` the task index within the job and `host` the machine's
node name (`local` for the controller), so every task's raw, unrounded sample is one `installs` row.
`npm-install-nofsync` emits the same tables without the flush and durable columns (`flush_mean_s`,
`durable_mean_s`, `batch_durable_s`, `durable_installs_per_s`, `flush_s`, `durable_s`).

| Metric | Meaning |
| --- | --- |
| `install_mean_s`, `flush_mean_s`, `durable_mean_s` | Mean `install_s`, `flush_s`, `durable_s` over the batch's tasks. |
| `batch_install_s` | Slowest task's own `install_s`. Cross-machine start offsets are not observable, so this is the slowest single install rather than a wall clock from release, and it excludes release skew. |
| `batch_durable_s` | The batch's barrier-to-barrier wall time: from the release barrier to the barrier after every task's flush, so it includes release skew and every task's flush. Only with `flush`. |
| `installs_per_s` / `durable_installs_per_s` | `tasks / batch_install_s` / `tasks / batch_durable_s`. |
| `user_s`, `sys_s` | Summed child CPU time, from each task's own `RUSAGE_CHILDREN` delta. |
| `files`, `dirs`, `symlinks`, `hardlinks`, `logical_bytes`, `allocated_bytes`, `du_kib` | Summed tree inventory over the batch's tasks. |

Means describe one install, batch times describe the batch, and throughput is a batch quantity,
never a per-install latency. Clocks are never compared across machines: every duration is measured
inside the task that produced it, and the only cross-task time is the barrier wall that the
launcher's own barriers delimit.

Discovered facts live in `manifest.json`: `targets[].info.hosts[<node>]` holds that machine's
`target_fstype`, `prep_fstype`, `tool_versions` and `seed_input_sha256` of the committed
template inputs; `targets[].path` holds the target directory, `run_id` the run, and `config` the run
settings (`fixture`, `pm`, `repeats`, `tasks`, `cpus_per_task`, `oversubscribe`, `concurrency`,
`prep_dir`, `ignore_scripts`, `flush`, `keep_node_modules`, `seed_only`, executable names, version
pins). Device IDs and resolved executable paths are not stored. `plot` pools repeated runs only when
their configs and source fingerprints match; a non-zero exit, missing tree, walk or fsync failure,
or failed `du` aborts the whole job and emits no row for that repeat.

## Figures

`run` renders the default figures into `results/npm-install/<run-id>/figures/`, one file per
manager; `python -m agent_fs_bench plot <run-dir>...` re-renders them.

| Figure | Table | X | Y |
| --- | --- | --- | --- |
| `batches-install_mean_s-<manager>` | `batches` | `tasks` | `install_mean_s` |
| `batches-durable_mean_s-<manager>` | `batches` | `tasks` | `durable_mean_s` |

Each point is the median over repeats with IQR error bars, the x axis is logarithmic, and the series
is the target, so several targets of one run share a figure. `npm-install-nofsync` renders only the
install-time figure. Because `tasks` is one value per run, a curve needs runs pooled with
`--vary tasks`, and a run holds only one `tasks` value. Latency and throughput stay in separate
figures because one is per install and the other per batch. A one-off figure comes from the same
tables: `python -m agent_fs_bench plot results/npm-install/<run-id> --table batches --x tasks --y
installs_per_s --split manager --ylabel 'Installs/s'`. This repository commits no generated plots
and never invents data to fill one.

## Notes

The timed install reads the preparation filesystem every run, so keep it local and quiet on every
machine. `install_s` includes process startup, so increase `repeats` rather than shrinking the
workload. The benchmark root stays on the target; delete it yourself. Changing the timed command,
flush protocol, CPU pin or isolation changes `source_sha256`; select one source identity rather than
pooling old and new measurements.
