# Mount scale: client scaling over the run-mode machines

Purpose: hold per-client small-file work fixed and, at one chosen concurrency N, compare file-operation throughput, lifecycle cost and per-machine resource cost across independent mount topologies. It uses **upstream IOR 4.0.0 mdtest + Open MPI 4.1.x**; this is a proxy workload for agent file operations, not an end-to-end agent benchmark.

Entry point: `agent_fs_bench/benchmarks/mount_scale.py`.
- `mount_scale_cluster.py` holds the persistent per-machine lifecycle workers.
- `mount_scale_mdtest.py` handles native timing of the global MPI jobs.
- Placement, machine probing and the launch wrapper come from the shared `agent_fs_bench/cluster.py` ([run modes](run-modes.md)).

## Experiment unit and placement

Each client is one single-threaded mdtest task. K is the **total number of directories per client**: one private workspace plus K-1 shared read-only resources, default K=3. Each shared resource has one underlying fixture; every client reaches it through its own alias.

| Topology | Actual filesystem mounts per client | New mount entries for N clients |
| --- | --- | --- |
| `multi-mount` | One private workspace and one per shared resource | N×K |
| `shared-directory` | One subdir mount; the LakeBase `--bind` maps shared resources inside it | N |
| `directory-smoke` | Ordinary directories, no mounts | No mount measurements |

`shared-directory` does not use one host-level mount shared by all clients. LakeBase's internal `--bind` is also not a Linux bind mount. K=3 means three directories; only `multi-mount` may be described as "each client performs three filesystem mounts".

**One run measures one N; there is no default sweep and no automatic N=1 addition.** `run --mode` selects the machines:
- `local`: the controller;
- `overflow`: the controller, then `local/hosts.toml` in file order;
- `remote`: only `local/hosts.toml`, and the controller hosts no client.

`CONFIG['tasks']`:

- `None` (default): N is every available logical CPU of the selected machines, one client each.
- Positive integer: measure only that N. Machines fill in order, and only machines that receive clients start a lifecycle worker. N above the probed total capacity is an error; the run never oversubscribes or shrinks the experiment.
- "Available logical CPU" comes from each machine's `os.sched_getaffinity(0)`, not physical core count. Real CPU IDs are kept and assigned in ascending order. Before exec, the cluster wrapper pins each task to one CPU and verifies the actual hostname, global rank and affinity; any mismatch with the plan fails the run.

Default full-core placement is one task per available logical CPU; it does not guarantee 100% CPU utilization, because filesystem waits can leave a CPU idle. No core is reserved for the controller: Python, MPI, the kernel and filesystem daemons still share CPUs with tasks. Formal experiments should fix cpuset, SMT, NUMA, machine model and background load.

For example, take 64 available logical CPUs on the controller and 64 on each of two listed machines.
- `overflow` mode: the default N is 192. An explicit `tasks = 100` allocates controller=64 and first machine=36; the second machine does not participate.
- `remote` mode: the same request allocates 64 on the first machine and 36 on the second.

To draw a scaling curve, choose N explicitly in separate runs and pool them with `plot --vary tasks`; this implementation does not decide paper sweep points.

## Dependencies and network

Every participating machine needs:
- Linux and Python 3.12+;
- this repository's checkout with `uv sync --locked`, at the same path by default;
- the **same mdtest binary and Open MPI 4.1.x version on the same architecture**.

The controller also needs the Arial font. `run` renders the default figures after the last measurement, so a missing font fails the run only after the CSV tables are written (re-plot them elsewhere with `plot`). mdtest must link this Open MPI; other MPI implementations are not supported. The benchmark does not install tools or use sudo. Machine setup, the inventory, launcher settings and placement proofs: [run modes](run-modes.md).

Build upstream IOR as follows:

```sh
# Unpack the official ior-4.0.0.tar.gz and enter its source directory.
./configure CC=mpicc
make -j
# Install src/mdtest on each machine; prefer an absolute CONFIG['mdtest'], because
# Open MPI does not read shell profiles on remote machines.
```

An intra-cloud cluster needs:

1. Non-interactive SSH from the controller to each worker, with host keys already trusted. Custom user, port, identity and ProxyJump come from the hosts inventory.
2. Bidirectional MPI TCP reachability between participating machines, and resolution of the hostnames/addresses MPI uses. The minimal setup allows group-internal TCP only to the experiment machines' security group members or private IPs, and checks the OS firewall; do not expose it to the public internet.
3. Open MPI startup, barriers and task communication do not use a benchmark-built SSH tunnel. Opening TCP 22 alone is not enough. Force plain TCP and same-host shared memory; RDMA is not required. Do not mistake the launcher port range for the range of all MPI traffic.
4. On multi-NIC machines, select mutually reachable MPI data/control interfaces explicitly in `CONFIG['mpiexec_args']` of `agent_fs_bench/cluster.py`, and archive it. MPI traffic can compete with storage traffic, so report the network layout.

## Configuration and runs

Experiment parameters live in `CONFIG` at the top of `mount_scale.py`; there are no scaling or fault-tolerance CLI flags.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `tasks` | `None` | Single N; default is every available logical CPU of the `--mode` machines |
| `profiles` | `local/mounts.toml` | Target mount profile, read on the controller |
| `directories` | `3` | K, including the private workspace; may be 1 |
| `files` | `1000` | File operations per client per directory |
| `file_size_bytes` | `4096` | Bytes created/read per file |
| `repeats` | `3` | Complete repetitions at the same N |
| `mdtest` | `mdtest` | Upstream binary, the same on every machine; prefer one absolute path |
| `command_timeout_s` | `300` | Timeout for each mount/unmount command, each MPI job and worker control operations |
| `idle_sample_s` | `1.0` | Machine idle sampling duration for baseline/mounted/released |
| `resource_sample_s` | `0.1` | Machine resource sampling period during the workload |

1000 files of 4 KiB are only a light default. Formal experiments need phase durations well above MPI barrier, timer and scheduling overhead; do not claim peak throughput from a very short smoke.

### Target and mount profile

The target is a **local staging root at the same absolute path on every machine**. It must already exist and be outside the admin volume. Results still go to `results/mount-scale/<run-id>/` in the controller repository. Each machine's lifecycle worker runs from its own checkout and works under `<target>/agent-fs-bench/mount-scale/<run-id>/<node>/`.

Copy and edit `experiments/mounts.example.toml` to `local/mounts.toml` on the controller. The controller sends this private profile as text to each worker over the job's stdin, so no copy is written on other machines; all participating machines use the same target profile. External tools, mount permissions and admin mounts are provisioned by the experiment environment.

`admin_root` must be pre-mounted at the same path on every machine as a **full admin view of the same volume**. Prepared data lives under `<admin_root>/agent-fs-bench/mount-scale/<run-id>/`:
- The first participating machine's worker creates it, with this run's random owner sentinel and the shared resources.
- Every worker reads the sentinel before setup and rejects a disconnected admin namespace.
- Per-repeat private data is further separated by logical machine name to avoid cross-machine collisions; agent numbering starts at 0 on each machine.

Mount profile argv is expanded with `format_map`, without a shell, using these placeholders:

| Placeholder | Content |
| --- | --- |
| `{source}` | Volume-internal path starting with `/`, relative to the admin volume root |
| `{mountpoint}` | Actual mountpoint on the current machine |
| `{subdir}` | Private volume-internal path; workspace for multi-mount, agent root for shared-directory |
| `{binds}` | Comma-separated volume-absolute `<shared source>:<agent shared-N>` mappings |
| `{agent}` | Client number on the current machine, starting at 0 |

The NFS profile keeps normal cache defaults and does not silently add `nosharecache`. N×K mount entries are not that many kernel clients, connections or caches. A FUSE profile must ensure each mount creates a real filesystem client; one mount plus a Linux bind cannot stand in for multiple clients.

Mount commands must return after the mount is established, and unmount commands after it is released. The shared workload is read-only; this does not prove enforced permission isolation. With K=1, remove the empty `--bind` argument from the shared-directory profile.

```sh
uv run python -m agent_fs_bench run mount-scale --mode overflow --target nfs
uv run python -m agent_fs_bench run mount-scale --mode overflow --target lakebase

# Two clients, ordinary directories, 32 files, one repeat; reads no profile and mounts nothing.
mkdir -p /tmp/mdtest-directory-smoke
uv run python -m agent_fs_bench run mount-scale --mode local \
  --dirs /tmp/mdtest-directory-smoke -- --directory-smoke
```

`--directory-smoke` keeps its ordinary data inside the staging work directory, so it runs on one machine only. It needs at least two available logical CPUs on the selected machines; otherwise it fails before starting workers/MPI and never shrinks to N=1. This checks `sched_getaffinity`, not a container CPU-time quota.

## Execution and timing boundaries

1. Allocate N one-CPU tasks over the selected machines from the cluster probe (affinity CPU IDs, hostname, kernel, architecture, Python, Open MPI library, source fingerprint), and start one persistent lifecycle worker on each participating machine (over SSH for listed machines).
2. The first machine's worker creates the run's data root, owner sentinel and shared resources; one mdtest task on the first slot creates each shared fixture in that machine's admin view. In each repeat, every machine creates its own private workspace, sentinels and mountpoints, then samples baseline; this preparation is outside mount command timing.
3. The controller tells all workers to mount concurrently. Each machine starts its local clients concurrently, and each client runs its mount commands in order: private first, then each shared resource for multi-mount. Each machine verifies that new mount entries exactly match the plan and that sentinel aliases are correct. Mounted-idle is sampled before shared warm-up.
4. For each shared resource, start one **global MPI job** that stats and reads it; discard that round's performance values.
5. Start per-machine resource sampling, then start the global private mdtest job `create → stat → read → remove`; then run one job per shared resource for `stat → read`. All tasks use mdtest's own phase barriers in one `MPI_COMM_WORLD`; throughput is not measured per machine and summed. Private/shared and each shared resource remain separate phases, not an interleaved mixed workload.
6. Each task is explicitly assigned a machine and CPU through the Open MPI appfile. The cluster wrapper:
   - verifies the global rank, actual hostname and single-CPU affinity;
   - requires the task's client aliases to exist on its own machine;
   - reports the binary's SHA-256, then execs upstream mdtest.

   The controller checks that the measured affinity is exactly the requested single CPU and that every task ran the same binary. The private `test-dir.0-0` is precreated on its machine outside timing. Global rank 0 writes the native CSV on its own machine; the wrapper returns it through the launcher output, so no machine needs a shared result disk.
7. Stop resource sampling, unmount concurrently in reverse order on each machine, verify no residue, and sample released-idle. On success, emit the whole repeat's results and delete the unmounted private data and the empty repeat directory. After all repeats succeed, collect lifecycle logs; then the first machine's worker deletes this run's shared fixture.

Protocol: warm shared data, no cache drop; prereading does not guarantee later cache residency. Private reads follow this round's create with no fsync/sync, so they do not prove durable-write latency. The mdtest wrapper, loop and barriers are inside upstream phase timing; the Python wrapper, MPI startup, worker startup, fixture, warm-up and result output are outside it.

`--warningAsErrors` is enabled. Any of the following fails the run, and the repeat emits no successful throughput:
- operation errors;
- wrong task/node counts or actual placement;
- a version or binary mismatch;
- missing or nonfinite timings.

A failure stops the run and leaves mounts and data in place for inspection: no automatic unmount, retry, failover, or shrinking to a smaller N. Inspect every machine before handling that run.

## Results and interpretation

Each run writes `results/mount-scale/<run-id>/manifest.json` (run configuration, mode, machine facts and per-target discovered facts) plus one flat CSV per record kind. `run` also writes `summary/<table>.csv` for every table with a `repeat` or `sample` column and renders the default figures into `figures/`.

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | target, topology, tasks, phase, repeat | ops, elapsed_s, ops_per_s, ops_per_s_per_task, rank_mean_us_per_op, bytes, mib_per_s |
| `ranks` | target, topology, tasks, phase, repeat, sample, host, cpu | elapsed_s |
| `mounts` | target, topology, tasks, action, repeat | controller_dispatch_elapsed_s, measured_mounts, remaining_mounts |
| `lifecycle` | target, topology, tasks, host, action, repeat | elapsed_s, measured_mounts, remaining_mounts |
| `agents` | target, topology, tasks, host, action, repeat, sample | elapsed_s |
| `resources` | target, topology, tasks, host, state, repeat | levels, interval, deltas and peaks (below) |

Column values:
- `phase`: `private/create`, `private/stat`, `private/read`, `private/remove`, or `shared-<n>/stat`/`shared-<n>/read`.
- `action`: `mount` or `unmount`.
- `state`: `baseline`, `mounted-idle`, `released-idle` or `workload`.
- In `ranks`, `sample` is the global mdtest rank index and `host`/`cpu` are that task's logical machine and pinned CPU.
- In `agents`, `sample` is the client index on that machine.

Metric cells that do not apply to a row stay empty: for example `remaining_mounts` on a mount row, `bytes` on a stat phase, deltas on the baseline, and peaks on a non-workload state. `summary/phases.csv` therefore has one row per target × topology × tasks × phase with `n` and each metric's median; `summary/resources.csv` has one row per target × topology × tasks × host × state.

| Metric | Definition |
| --- | --- |
| `phases.ops` | N×files |
| `phases.elapsed_s` | Upstream rank CSV all-row phase time, including the phase-end barrier; not subprocess wall time |
| `phases.ops_per_s` | N×files / elapsed_s; per-machine rates are never summed |
| `phases.ops_per_s_per_task` | Aggregate throughput / N; not an independent per-client sample |
| `phases.rank_mean_us_per_op` | sum(task times)/(N×files)×1e6; phase mean, not per-operation latency/p99 |
| `phases.bytes`, `phases.mib_per_s` | Logical create/read bytes; shared data is read N times, not physical network/disk bytes |
| `ranks.elapsed_s` | One task's time before the phase-end barrier |
| `lifecycle.elapsed_s` | Wall time of all client lifecycle commands on that machine; excludes alias/mountinfo verification |
| `agents.elapsed_s` | One client's mount or unmount command sequence on that machine |
| `mounts.controller_dispatch_elapsed_s` | From notifying all workers to receiving all replies, **including the control channel, scheduling and mount verification**; not pure mount time and not a sum of per-machine times |
| `measured_mounts`, `remaining_mounts` | New/residual entries after per-machine verification; global rows are sums, not inferred connection or daemon counts |

`scaling_efficiency` is not emitted: a single-point run has no same-round N=1 baseline. If a paper needs efficiency, choose a comparable baseline in separate runs; the harness does not add experiment points implicitly.

`manifest.json` stores the parsed configuration and `mode` once, the launcher version, and each machine's probed facts (`nodes`: hostname, kernel, architecture, Python, mpi4py, Open MPI library, CPU affinity list). `targets[i].info` stores:
- `hosts`: the CPUs assigned to each participating machine;
- the mdtest SHA-256 and version;
- the mount profile SHA-256;
- `placement`: the verified placement proof per run case (`N<tasks>-R<repeat>`) and mdtest job, with each task's index, logical machine, hostname and CPUs.

`ranks` carries each task's planned machine and CPU for every phase; the wrapper's proof fails the run on any machine/CPU mismatch. SSH transport configuration and mount credentials are not written; archive inventory/profile privately. mdtest stdout/stderr, appfiles and the wrapper's placement lines remain as diagnostic attachments.

Default figures are rendered when their table has rows, so the directory smoke, which mounts nothing, skips `mount-dispatch`. Re-render them with `uv run python -m agent_fs_bench plot <run-dir>...`.

| Figure stem | Table | x | y | Split |
| --- | --- | --- | --- | --- |
| `throughput-<topology>-<phase>` | `phases` | tasks | ops_per_s | topology, phase |
| `mount-dispatch-<topology>-<action>` | `mounts` | tasks | controller_dispatch_elapsed_s | topology, action |
| `memory-delta-<topology>` | `resources` | tasks | used_bytes_delta for `mounted-idle` | topology |

Each run measures one N, so its default figures hold one x point. Pool runs of different N into scaling curves with `uv run python -m agent_fs_bench plot <run-N1> <run-N2>... --vary tasks`. All other configuration, the mode and the source fingerprint must match; the fingerprint ignores each module's `CONFIG` literal, so editing `CONFIG['tasks']` between runs is fine. Line figures draw the median and IQR of all matching rows at each x.

Resources come from **whole-machine counters on each machine**: CPU busy, MemTotal−MemAvailable, cache, dirty, slab, etc. The columns per `state`:

| Rows | Columns |
| --- | --- |
| `baseline`, `mounted-idle`, `released-idle` | Raw levels: `used_bytes`, `cached_bytes`, `dirty_bytes`, `slab_bytes`, `slab_reclaimable_bytes`, `slab_unreclaimable_bytes`, `mount_entries` |
| Every row | Sampling interval: `sample_elapsed_s`, `cpu_busy_s`, `cpu_busy_cores`, `cpu_busy_percent` |
| `mounted-idle`, `released-idle` | Also `used_bytes_delta`, `slab_bytes_delta`, `slab_unreclaimable_bytes_delta` and `mount_entries_delta` against `baseline` |
| `workload` | No raw levels; observed `peak_*` levels and `peak_*_delta` |

Counters include Python, MPI, daemons, the kernel and other processes; there is no filesystem CPU/RSS attribution. Sampling windows include MPI startup/shutdown and phase gaps, and control timing differs across machines; do not add per-machine peaks and call it a simultaneous global peak. Mount entries belong to the worker's current namespace, so a daemon must create mounts in that namespace.

## Paper claim boundaries

- Keep per-client work fixed; only increasing N across runs constitutes weak scaling. In `overflow` and `remote` modes, adding machines also adds client CPU, memory and network resources. Report placement, hardware and the controller-to-cross-machine inflection point, and do not attribute all differences to mount count.
- Distinguish the same-backend `multi-mount` vs `shared-directory` ablation from cross-NAS comparisons; cross-system comparisons also mix server, cache, RPC and permission/consistency policy differences.
- Some measured systems may cap mounts at 1000; do not hardcode that as a capacity rule for all systems. With K=3, 333/334 agents correspond to 999/1002 **new experimental mount entries**; admin/other sessions may also consume provider quota. This round measures only successful-point performance, not rejection-boundary probing/recovery; do not claim "verified to reject here" from an unrun high N.
- By default targets run in order, without randomization; formal runs should interleave target order, retain repeat-sample dispersion, and check cache/slab history and background noise. Short directory/bind smokes are not paper performance data.
- No containers, models or real agents are started; there are no per-operation tail latency, durability or malicious-write isolation conclusions.

## Validation performed

Smoke runs of this implementation:
- colima: non-root, Ubuntu 24.04 aarch64, 6 CPUs, Python 3.12.3, Open MPI 4.1.6.
- Two lab servers: Ubuntu 22.04 x86_64, 40 CPUs each, Open MPI 4.1.2, no shared filesystem.

mdtest was built from the official IOR 4.0.0 release.

| Scenario | Observed result |
| --- | --- |
| colima `--mode local`, `--directory-smoke`, N=2, K=3, 32 files | 28 rows: 8 `phases`, 16 `ranks`, 4 `resources`; summaries; the 9 default figures render from these tables |
| Lab servers `--mode remote`, `--directory-smoke`, N=2 | Controller hosts no client; one lifecycle worker over SSH and the mdtest jobs ran on the second server; 28 rows, task 0's CSV returned through the launcher output |

Directory smokes **only validate the harness/lifecycle**, not independent NAS clients or storage performance; these results are not paper data. Real NFS or LakeBase profiles, a cross-machine admin view and thousand-client experiments have not been run with this implementation; do not claim they passed performance or capacity validation.

## References

- [IOR 4.0.0 release](https://github.com/hpc/ior/releases/tag/4.0.0) and [mdtest native timing/CSV source](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) and [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
- [Linux NFS mount/cache semantics](https://man7.org/linux/man-pages/man5/nfs.5.html)
