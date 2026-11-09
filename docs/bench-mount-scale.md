# Mount scale: local-first cross-host client scaling

Purpose: hold per-client small-file work fixed and, at one chosen concurrency N, compare file-operation throughput, lifecycle cost and per-host resource cost across independent mount topologies. It uses **upstream IOR 4.0.0 mdtest + Open MPI 4.1.x**; this is a proxy workload for agent file operations, not an end-to-end agent benchmark.

Entry point: `agent_fs_bench/benchmarks/mount_scale.py`. `mount_scale_cluster.py` handles CPU allocation and local/SSH lifecycle workers; `mount_scale_mdtest.py` handles native timing for one global MPI job. The independent Python worker/shared-write contention protocol of `multi-host` does not participate in this throughput calculation.

## Experiment unit and placement

Each client is one single-threaded mdtest rank. K is the **total number of directories per client**: one private workspace plus K-1 shared read-only resources, default K=3. Each shared resource has one underlying fixture; every client reaches it through its own alias.

| Topology | Actual filesystem mounts per client | New mount entries for N clients |
| --- | --- | --- |
| `multi-mount` | One private workspace and one per shared resource | N×K |
| `shared-directory` | One subdir mount; the LakeBase `--bind` maps shared resources inside it | N |
| `directory-smoke` | Ordinary directories, no mounts | No mount measurements |

`shared-directory` does not use one host-level mount shared by all clients. LakeBase's internal `--bind` is also not a Linux bind mount. K=3 means three directories; only `multi-mount` may be described as "each client performs three filesystem mounts".

**One run measures one N; there is no default sweep and no automatic N=1 addition.** `CONFIG['clients']`:

- `None` (default): N is the total available logical CPUs of the current Linux controller plus every machine in the hosts list.
- Positive integer: measure only that N. Fill the current Linux controller first, then machines in hosts-file order; stop connecting once N is reached. N above the probed total capacity is an error; the run never oversubscribes or shrinks the experiment.
- "Available logical CPU" comes from each worker's `os.sched_getaffinity(0)`, not physical core count. Real CPU IDs are kept and assigned in ascending order. Before exec, the rank wrapper pins affinity to one CPU and verifies the actual hostname, global rank and affinity; any mismatch with the plan fails the run.

Default full-core placement is one rank per available logical CPU; it does not guarantee 100% CPU utilization, because filesystem waits can leave a CPU idle. No core is reserved for the controller: Python, MPI, the kernel and filesystem daemons still share host CPUs with ranks. Formal experiments should fix cpuset, SMT, NUMA, machine model and background load.

For example, with 64 available logical CPUs locally and 64 on each of two remote machines: default N=192; explicit N=100 allocates local=64, first remote=36, and the second remote does not participate. To draw a scaling curve, the experimenter chooses N explicitly in separate runs; this implementation does not decide paper sweep points.

## Dependencies and network

Every participating machine preinstalls Linux, Python 3.12+, and the **same mdtest binary and Open MPI 4.1.x version on the same architecture**. The controller also needs the repository and its Python environment. mdtest must link this Open MPI; other MPI implementations are not supported. The benchmark does not install tools or use sudo.

Build upstream IOR as follows:

```sh
# Unpack the official ior-4.0.0.tar.gz and enter its source directory.
./configure CC=mpicc
make -j
# Put src/mdtest and Open MPI's mpiexec on each machine's non-interactive PATH.
```

An intra-cloud cluster needs:

1. Non-interactive SSH from the controller to each worker, with host keys already trusted. Custom user, port, identity and ProxyJump come from the hosts configuration.
2. Bidirectional MPI TCP reachability between participating machines, and resolution of the hostnames/addresses MPI uses. The minimal setup allows group-internal TCP only to the experiment machines' security group members or private IPs, and checks the OS firewall; do not expose it to the public internet.
3. Open MPI startup, barriers and rank communication do not use a benchmark-built SSH tunnel. Opening TCP 22 alone is not enough. Force plain TCP and same-host shared memory; RDMA is not required. Do not mistake the launcher port range for the range of all MPI traffic.
4. On multi-NIC machines, select mutually reachable MPI data/control interfaces explicitly in `CONFIG['mpi_launcher']`, and archive the experiment configuration. MPI traffic can compete with storage traffic, so report the network layout.

Open MPI starts remote daemons through the inventory-aware SSH agent on the controller with tree spawning disabled; workers do not need each other's private keys. The agent uses the existing hosts/SSH rules in `agent_fs_bench/ssh.py`. Open MPI host tokens must be reachable hostnames or IPv4 addresses; an SSH-only alias cannot stand in for a resolvable MPI address. The controller Python, checkout and inventory paths must not contain whitespace or colons; otherwise Open MPI 4 cannot parse `plm_rsh_agent` and fails to start.

## Configuration and runs

Experiment parameters live in `CONFIG` at the top of `mount_scale.py`; there are no scaling or fault-tolerance CLI flags.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `clients` | `None` | Single N; default is the total available logical CPUs of all listed hosts |
| `hosts` | `local/hosts.toml` | Ordered remote SSH inventory; excludes the current controller |
| `profiles` | `local/mounts.toml` | Target mount profile |
| `directories` | `3` | K, including the private workspace; may be 1 |
| `files` | `1000` | File operations per client per directory |
| `file_size_bytes` | `4096` | Bytes created/read per file |
| `repeats` | `3` | Complete repetitions at the same N |
| `mdtest` | `mdtest` | Upstream binary on each machine's non-interactive PATH, or one absolute path |
| `mpi_launcher` | `('mpiexec',)` | Open MPI launcher; the harness generates host/slot/appfile arguments |
| `connect_timeout_s` | `15` | SSH connect timeout |
| `command_timeout_s` | `300` | Timeout for each mount/unmount command, the MPI job and worker control operations |
| `idle_sample_s` | `1.0` | Host idle sampling duration for baseline/mounted/released |
| `resource_sample_s` | `0.1` | Host resource sampling period during the workload |

1000 files of 4 KiB are only a light default. Formal experiments need phase durations well above MPI barrier, timer and scheduling overhead; do not claim peak throughput from a very short smoke.

### SSH hosts

Same format as `experiments/hosts.example.toml` for `multi-host`, but **list remote machines only; order is fill order**:

```toml
[[hosts]]
name = "node-a"
address = "10.0.0.11"
user = "bench"
port = 22
python = "python3"
ssh_args = ["-i", "/home/operator/.ssh/bench_ed25519"]

[[hosts]]
name = "node-b"
address = "10.0.0.12"
user = "bench"
```

`name='local'` is reserved for the controller. Do not list the same machine or SSH endpoint twice; the run also rejects duplicate execution hostnames. To use `clients=None` but fill only local CPUs, write `hosts = []` in the inventory file. When an explicit N fits within local capacity, or when running the directory smoke, the hosts file is not read; otherwise a missing file is an error, not a silent local-only fallback.

### Target and mount profile

The target is a **local staging root at the same absolute path on every machine**. It must already exist and be outside the admin volume. Results still go to `results/mount-scale/<run-id>/` in the controller repository. Copy and edit `experiments/mounts.example.toml` to `local/mounts.toml`; all participating machines use the same target profile. The controller distributes this run's Python package and private profile to remote staging; no remote checkout is needed. External tools, mount permissions and admin mounts are provisioned by the experiment environment.

`admin_root` must be pre-mounted at the same path on every machine as a **full admin view of the same volume**. Prepared data lives under `<admin_root>/agent-fs-bench/mount-scale/<run-id>/`; each worker reads this run's random owner sentinel and rejects a disconnected admin namespace. Per-repeat private data is further separated by logical host name to avoid cross-machine collisions; agent numbering starts at 0 on each host.

Mount profile argv is expanded with `format_map`, without a shell, using these placeholders:

| Placeholder | Content |
| --- | --- |
| `{source}` | Volume-internal path starting with `/`, relative to the admin volume root |
| `{mountpoint}` | Actual mountpoint on the current machine |
| `{subdir}` | Private volume-internal path; workspace for multi-mount, agent root for shared-directory |
| `{binds}` | Comma-separated volume-absolute `<shared source>:<agent shared-N>` mappings |
| `{agent}` | Client number on the current host, starting at 0 |

The NFS profile keeps normal cache defaults and does not silently add `nosharecache`. N×K mount entries are not that many kernel clients, connections or caches. A FUSE profile must ensure each mount creates a real filesystem client; one mount plus a Linux bind cannot stand in for multiple clients.

Mount commands must return after the mount is established, and unmount commands after it is released. The shared workload is read-only; this does not prove enforced permission isolation. With K=1, remove the empty `--bind` argument from the shared-directory profile.

```sh
uv run python -m agent_fs_bench run mount-scale --target nfs
uv run python -m agent_fs_bench run mount-scale --target lakebase

# Two local clients, ordinary directories, 32 files, one repeat; reads no hosts/profile and mounts nothing.
mkdir -p /tmp/mdtest-directory-smoke
uv run python -m agent_fs_bench run mount-scale \
  --dirs /tmp/mdtest-directory-smoke -- --directory-smoke
```

`--directory-smoke` requires at least two available logical CPUs in the current affinity set; otherwise it fails before starting workers/MPI and never shrinks to N=1. This checks `sched_getaffinity`, not a container CPU-time quota.

## Execution and timing boundaries

1. Start persistent lifecycle workers on the participating machines; probe affinity CPU IDs, kernel, architecture, Python, mdtest SHA256 and MPI version; produce the local-first single-point N placement.
2. The controller creates the shared fixture once, using one mdtest rank to create it in the admin view. In each repeat, every host creates its own private workspace, sentinels and mountpoints, then samples baseline; this preparation is outside mount command timing.
3. The controller tells all hosts to mount concurrently; each host starts its local clients concurrently, and each client runs its mount commands in order: private first, then each shared resource for multi-mount. Each host verifies that new mount entries exactly match the plan and that sentinel aliases are correct. Mounted-idle is sampled before shared warm-up.
4. For each shared resource, start one **global MPI job** that stats and reads it; discard that round's performance values.
5. Start per-host resource sampling, then start the global private mdtest job `create → stat → read → remove`; then run one job per shared resource for `stat → read`. All ranks use mdtest's own phase barriers in one `MPI_COMM_WORLD`; throughput is not measured per machine and summed. Private/shared and each shared resource remain separate phases, not an interleaved mixed workload.
6. Each rank is explicitly assigned a host through the Open MPI appfile; the wrapper verifies global rank, actual hostname and single-CPU affinity, then execs upstream mdtest. The controller additionally checks that the proof's measured `affinity` is exactly the requested single CPU, not just the echoed `cpu`. The private `test-dir.0-0` is precreated on its host outside timing; only rank 0 writes the global native CSV to the controller result directory, so remote hosts need no shared result disk.
7. Stop resource sampling, unmount concurrently in reverse order on each host, verify no residue, and sample released-idle. On success, emit the whole repeat's results and delete the unmounted private data; after all repeats succeed, collect lifecycle logs and delete this run's shared fixture and remote deployment.

Protocol: warm shared data, no cache drop; prereading does not guarantee later cache residency. Private reads follow this round's create with no fsync/sync, so they do not prove durable-write latency. The mdtest wrapper, loop and barriers are inside upstream phase timing; the Python wrapper, MPI startup, deployment, fixture, warm-up and result output are outside it.

`--warningAsErrors` is enabled. Operation errors, wrong rank/node counts or actual placement, version/binary mismatch, and missing or nonfinite timings all fail; the repeat emits no successful throughput. A failure stops the run and leaves mounts and data in place for inspection: no automatic unmount, retry, failover, or shrinking to a smaller N. Inspect every machine before handling that run.

## Results and interpretation

Each run writes `results/mount-scale/<run-id>/manifest.json` (run configuration and per-host discovered facts, once) plus one flat CSV per record kind. `run` also writes `summary/<table>.csv` for every table with a `repeat` or `sample` column and renders the default figures into `figures/`.

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | topology, clients, phase, repeat | ops, elapsed_s, ops_per_s, ops_per_s_per_client, rank_mean_us_per_op, bytes, mib_per_s |
| `ranks` | topology, clients, phase, repeat, sample, host, cpu | elapsed_s |
| `mounts` | topology, clients, action, repeat | controller_dispatch_elapsed_s, measured_mounts, remaining_mounts |
| `lifecycle` | topology, clients, host, action, repeat | elapsed_s, measured_mounts, remaining_mounts |
| `agents` | topology, clients, host, action, repeat, sample | elapsed_s |
| `resources` | topology, clients, host, state, repeat | levels, interval, deltas and peaks (below) |

`phase` is `private/create`, `private/stat`, `private/read`, `private/remove`, or `shared-<n>/stat`/`shared-<n>/read`. `action` is `mount` or `unmount`; `state` is `baseline`, `mounted-idle`, `released-idle` or `workload`. In `ranks`, `sample` is the global mdtest rank index and `host`/`cpu` are that rank's logical host and pinned CPU; in `agents`, `sample` is the client index on that host. Metric cells that do not apply to a row stay empty (for example `remaining_mounts` on a mount row, `bytes` on a stat phase, deltas on the baseline, peaks on a non-workload state). `summary/phases.csv` therefore has one row per target × topology × clients × phase with `n` and each metric's median; `summary/resources.csv` has one row per target × topology × clients × host × state.

| Metric | Definition |
| --- | --- |
| `phases.ops` | N×files |
| `phases.elapsed_s` | Upstream rank CSV all-row phase time, including the phase-end barrier; not subprocess wall time |
| `phases.ops_per_s` | N×files / elapsed_s; per-machine rates are never summed |
| `phases.ops_per_s_per_client` | Aggregate throughput / N; not an independent per-client sample |
| `phases.rank_mean_us_per_op` | sum(rank times)/(N×files)×1e6; phase mean, not per-operation latency/p99 |
| `phases.bytes`, `phases.mib_per_s` | Logical create/read bytes; shared data is read N times, not physical network/disk bytes |
| `ranks.elapsed_s` | One rank's time before the phase-end barrier |
| `lifecycle.elapsed_s` | Wall time of all client lifecycle commands on that host; excludes alias/mountinfo verification |
| `agents.elapsed_s` | One client's mount or unmount command sequence on that host |
| `mounts.controller_dispatch_elapsed_s` | From notifying all workers to receiving all replies, **including the control channel, scheduling and mount verification**; not pure mount time and not a sum of per-host times |
| `measured_mounts`, `remaining_mounts` | New/residual entries after per-host verification; global rows are sums, not inferred connection or daemon counts |

`scaling_efficiency` is not emitted: a single-point run has no same-round N=1 baseline. If a paper needs efficiency, choose a comparable baseline in separate runs; the harness does not add experiment points implicitly.

`manifest.json` stores the parsed configuration once; `targets[i].info` stores discovered facts: per-host hostname, kernel, architecture, Python, CPU affinity list and assigned CPUs; the mdtest binary path and SHA256; the Open MPI version; and the mount profile SHA256. The verified rank placement is not duplicated there: `ranks` carries each rank's host and CPU for every phase, and the wrapper's proof already fails the run on any host/CPU mismatch. SSH transport configuration and mount credentials are not written; archive inventory/profile privately. Raw placement proofs, mdtest stdout/stderr, CSV/appfile remain as diagnostic attachments.

Default figures (every run renders them, including the directory smoke; re-render with `uv run python -m agent_fs_bench plot <run-dir>...`):

| Figure stem | Table | x | y | Split |
| --- | --- | --- | --- | --- |
| `throughput-<topology>-<phase>` | `phases` | clients | ops_per_s | topology, phase |
| `mount-dispatch-<topology>-<action>` | `mounts` | clients | controller_dispatch_elapsed_s | topology, action |
| `memory-delta-<topology>` | `resources` | clients | used_bytes_delta for `mounted-idle` | topology |

Each run measures one N, so its default figures hold one x point. Pool runs of different N into scaling curves with `uv run python -m agent_fs_bench plot <run-N1> <run-N2>... --vary clients`; all other configuration must match. Line figures draw the median and IQR of all matching rows at each x.

Resources come from **whole-host counters on each machine**: CPU busy, MemTotal−MemAvailable, cache, dirty, slab, etc. Every `resources` row carries the raw levels (`used_bytes`, `cached_bytes`, `dirty_bytes`, `slab_bytes`, `slab_reclaimable_bytes`, `slab_unreclaimable_bytes`, `mount_entries`) and the sampling interval (`sample_elapsed_s`, `cpu_busy_s`, `cpu_busy_cores`, `cpu_busy_percent`); `mounted-idle` and `released-idle` additionally carry `used_bytes_delta`, `slab_bytes_delta`, `slab_unreclaimable_bytes_delta` and `mount_entries_delta` against `baseline`; `workload` carries observed `peak_*` levels and `peak_*_delta`. Counters include Python, MPI, daemons, the kernel and other processes; there is no filesystem CPU/RSS attribution. Sampling windows include MPI startup/shutdown and phase gaps, and control timing differs across machines; do not add per-host peaks and call it a simultaneous global peak. Mount entries belong to the worker's current namespace, so a daemon must create mounts in that namespace.

## Paper claim boundaries

- Keep per-client work fixed; only increasing N across runs constitutes weak scaling. Adding machines also adds client CPU, memory and network resources; report placement, hardware and the local-to-cross-host inflection point, and do not attribute all differences to mount count.
- Distinguish the same-backend `multi-mount` vs `shared-directory` ablation from cross-NAS comparisons; cross-system comparisons also mix server, cache, RPC and permission/consistency policy differences.
- Some measured systems may cap mounts at 1000; do not hardcode that as a capacity rule for all systems. With K=3, 333/334 agents correspond to 999/1002 **new experimental mount entries**; admin/other sessions may also consume provider quota. This round measures only successful-point performance, not rejection-boundary probing/recovery; do not claim "verified to reject here" from an unrun high N.
- By default targets run in order, without randomization; formal runs should interleave target order, retain repeat-sample dispersion, and check cache/slab history and background noise. Short directory/bind smokes are not paper performance data.
- No containers, models or real agents are started; there are no per-operation tail latency, durability or malicious-write isolation conclusions.

## Validation performed

Colima, non-root, Linux aarch64, Python 3.12.3, Open MPI 4.1.6, mdtest built from the official IOR 4.0.0 release:

| Scenario | Observed result |
| --- | --- |
| `--directory-smoke`, N=2, K=3, 32 files | `phases`, `ranks` and `resources` CSVs plus summaries and default figures generated |
| K=3 / K=1 directory runs after the placement fix | The K=3 `ranks` table carries host and CPU for private/shared-1/shared-2; K=1 over two consecutive rounds contains only private rows |
| `--directory-smoke` under `taskset` with only one logical CPU allowed | Fails explicitly requiring at least 2 available logical CPUs; starts no MPI and does not shrink implicitly |
| Default N, 6 available local logical CPUs, K=3 | Ranks pinned to CPUs 0–5; 18 dedicated Linux bind mounts created, mdtest completed, 0 residual after unmount |
| Explicit N=2, hosts file absent | Local only; full workload/lifecycle with 6 mounts succeeded |
| K=1, N=2, two repeats | Private create/stat/read/remove and two mount/unmount rounds succeeded |
| `taskset -c 1,3`, default N | Only two ranks, with actual affinity CPU 1 and 3; explicit N=3 rejected for insufficient capacity, no oversubscription |
| Real SSH worker from macOS→Colima | Package/profile distribution, preflight, 6 mounts, sampling, log collection, unmount with zero residue and deployment deletion succeeded; real SSH command execution through the inventory-aware MPI SSH agent succeeded |
| Repository unittest suite (after this round's fixes) | `uv run python -m unittest tests.test_mount_scale tests.test_mount_scale_cluster -v`: 22 passed, including regressions for affinity mismatch/missing rejection and the single-core smoke precondition |

Linux bind mounts **only validate the harness/lifecycle**, not independent NAS clients or storage performance. These temporary smoke results are not paper data. A global MPI job across two independent Linux hosts, real NFS or LakeBase profiles, and thousand-client experiments have not been run; do not claim they passed performance or capacity validation.

## References

- [IOR 4.0.0 release](https://github.com/hpc/ior/releases/tag/4.0.0) and [mdtest native timing/CSV source](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) and [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
- [Linux NFS mount/cache semantics](https://man7.org/linux/man-pages/man5/nfs.5.html)
