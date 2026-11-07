# Mount scale: local or remote-host client scaling

Purpose: hold per-client small-file work fixed and, at one chosen concurrency N, compare file-operation throughput, lifecycle cost and per-host resource cost across independent mount topologies. A second use adds M mounted-only idle clients per machine to measure what mount density costs the active clients. It uses **upstream IOR 4.0.0 mdtest + Open MPI 4.1.x**; this is a proxy workload for agent file operations, not an end-to-end agent benchmark.

Entry point: `agent_fs_bench/benchmarks/mount_scale.py`. `mount_scale_cluster.py` handles CPU reservation and local/SSH lifecycle workers; `mount_scale_mdtest.py` handles native timing for one global MPI job. The independent Python worker/shared-write contention protocol of `multi-host` does not participate in this throughput calculation.

## Experiment unit and placement

Each client is one single-threaded mdtest rank. K is the **total number of directories per client**: one private workspace plus K-1 shared read-only resources, default K=3. Each shared resource has one underlying fixture; every client reaches it through its own alias.

| Topology | Actual filesystem mounts per client | New mount entries for N clients |
| --- | --- | --- |
| `multi-mount` | One private workspace and one per shared resource | N×K |
| `shared-directory` | One subdir mount; the LakeBase `--bind` maps shared resources inside it | N |
| `directory-smoke` | Ordinary directories, no mounts | No mount measurements |

`shared-directory` does not use one host-level mount shared by all clients. LakeBase's internal `--bind` is also not a Linux bind mount. K=3 means three directories; only `multi-mount` may be described as "each client performs three filesystem mounts".

**One run measures one point; there is no default sweep and no automatic N=1 addition.** `CONFIG['mode']` selects one of two placements:

- `local`: every rank runs on the current Linux controller. `clients=None` uses every usable CPU; an integer N must fit in the usable CPUs. The hosts file is not read.
- `remote`: ranks run only on the first H machines of the hosts file (`remote_hosts`, `None` = all), one rank per usable CPU of each, so N = H × usable CPUs per host. Hosts must be homogeneous; a different usable CPU count fails the run. The controller launches MPI and creates the shared fixture through its own admin view, but runs no rank and mounts no client; listing the controller in the inventory is rejected. For a host-scaling curve, run once per H.

"Usable CPU" is each machine's `os.sched_getaffinity(0)` minus its `reserved_cpus` highest CPU ids (default 4, so a 64-vCPU host runs 60 ranks). Reserved CPUs never get a rank; filesystem daemons, kernel threads, orted, the worker and the sampler can run there. Open MPI sees exactly one rank per declared slot and therefore runs in aggressive mode: a rank that finishes a phase early spins at 100% CPU in the phase-end barrier. Without reserved CPUs that spin competes with FUSE daemons serving the slowest ranks and makes whole-host CPU counters meaningless. Real CPU IDs are kept and assigned in ascending order. Before exec, the rank wrapper pins affinity to one CPU and verifies the actual hostname, global rank and affinity; any mismatch with the plan fails the run.

Formal experiments should fix cpuset, SMT, NUMA, machine model and background load. `local` and `remote` are separate curves: do not join them into one N axis.

## Dependencies and network

Every participating machine preinstalls Linux, Python 3.12+, and the **same mdtest binary and Open MPI 4.1.x version on the same architecture**. The controller also needs the repository and DuckDB. mdtest must link this Open MPI; other MPI implementations are not supported. The benchmark does not install tools or use sudo.

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

Experiment parameters live in `CONFIG` at the top of `mount_scale.py`. The only workload CLI flags are `--directory-smoke` and `--idle-clients-per-host M` (default 0: no idle mounts); there are no scaling or fault-tolerance flags.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `mode` | `local` | `local`: ranks on the controller; `remote`: ranks only on SSH hosts |
| `clients` | `None` | `local` only: N; default is every usable local CPU |
| `remote_hosts` | `None` | `remote` only: H, the first H inventory hosts; default is all |
| `reserved_cpus` | `4` | Highest affinity CPU ids per machine that never get a rank |
| `hosts` | `local/hosts.toml` | Ordered remote SSH inventory; excludes the current controller |
| `profiles` | `local/mounts.toml` | Target mount profile |
| `directories` | `3` | K, including the private workspace; may be 1 |
| `files` | `1000` | File operations per client per directory |
| `file_size_bytes` | `4096` | Bytes created/read per file |
| `repeats` | `3` | Complete repetitions at the same point |
| `shared_seed` | `1` | Shared jobs run `mdtest -R --random-seed=<seed>`; rank r uses seed + r |
| `mdtest` | `mdtest` | Upstream binary on each machine's non-interactive PATH, or one absolute path |
| `mpi_launcher` | `('mpiexec',)` | Open MPI launcher; the harness generates host/slot/appfile arguments |
| `connect_timeout_s` | `15` | SSH connect timeout |
| `mount_timeout_s` | `300` | Each mount/unmount command and each worker control step |
| `mdtest_timeout_s` | `900` | Each global MPI job (warm-shared-i, shared-i, private) |
| `idle_sample_s` | `1.0` | Host idle sampling duration for baseline/mounted/released |
| `resource_sample_s` | `0.1` | Host resource sampling period during the workload |

1000 files of 4 KiB are only a light default. Formal experiments need phase durations well above MPI barrier, timer and scheduling overhead; do not claim peak throughput from a very short smoke.

### SSH hosts

Same format as `experiments/hosts.example.toml` for `multi-host`, but **list remote machines only; `remote` mode uses the first `remote_hosts` entries**:

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

`name='local'` is reserved for the controller. Do not list the same machine or SSH endpoint twice, and do not list the controller; the run rejects duplicate execution hostnames and a controller hostname among the hosts. `local` mode never reads the hosts file; `remote` mode requires it.

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

# Same run plus 200 mounted-only idle clients on every participating machine.
uv run python -m agent_fs_bench run mount-scale --target lakebase -- --idle-clients-per-host 200

# Two local clients, ordinary directories, 32 files, one repeat; reads no hosts/profile and mounts nothing.
mkdir -p /tmp/mdtest-directory-smoke
uv run python -m agent_fs_bench run mount-scale \
  --dirs /tmp/mdtest-directory-smoke -- --directory-smoke
```

`--directory-smoke` reserves one CPU and requires at least two usable logical CPUs left in the current affinity set; otherwise it fails before starting workers/MPI and never shrinks to N=1. This checks `sched_getaffinity`, not a container CPU-time quota. It cannot be combined with `--idle-clients-per-host`, which needs a mount profile.

## Execution and timing boundaries

1. Start persistent lifecycle workers on the participating machines (the controller in `local` mode, the first H hosts in `remote` mode); probe usable and reserved CPU IDs, kernel, architecture, Python, mdtest SHA256 and MPI version. The controller runs the same preflight and must match the hosts' mdtest, Open MPI and architecture.
2. The controller creates the shared fixture once, using one mdtest rank to create it in its admin view. In each repeat, every host creates private workspaces, sentinels and mountpoints for its active clients plus M idle clients, then samples baseline; this preparation is outside mount command timing.
3. The controller tells all hosts to mount concurrently; each host starts all its clients (active and idle) concurrently, and each client runs its mount commands in order: private first, then each shared resource for multi-mount. Each host verifies that new mount entries exactly match the plan and that sentinel aliases are correct. Mounted-idle is sampled before shared warm-up.
4. For each shared resource, start one **global MPI job** that stats and reads it through every active client's alias; discard that round's performance values.
5. Start per-host resource sampling, then run one job per shared resource for `stat → read`, then the global private mdtest job `create → stat → read → remove`. Shared jobs come first so the gap between warm-up and measurement does not depend on how long the private job took. Shared jobs (warm-up included) use `-R --random-seed=<shared_seed>`: mdtest adds the rank to the seed, so every rank stats and reads the same files in its own reproducible order. Without it all ranks open the same inode at the same moment after each barrier, which concentrates per-inode client/server state (NFSv4 OPEN state, Ceph capabilities) on one file at a time. All ranks use mdtest's own phase barriers in one `MPI_COMM_WORLD`; throughput is not measured per machine and summed. Private/shared and each shared resource remain separate phases, not an interleaved mixed workload.
6. Each rank is explicitly assigned a host through the Open MPI appfile; the wrapper verifies global rank, actual hostname and single-CPU affinity, then execs upstream mdtest. The controller additionally checks that the proof's measured `affinity` is exactly the requested single CPU, not just the echoed `cpu`. The private `test-dir.0-0` is precreated on its host outside timing. Only global rank 0 writes the native CSV: in `local` mode directly into the controller result directory; in `remote` mode into `<work_dir>/mdtest/` on the first host, from which the controller fetches it through that host's worker and keeps a copy with the job's diagnostics.
7. Stop resource sampling, unmount concurrently in reverse order on each host, verify no residue, and sample released-idle. On success, emit the whole repeat's results and delete the unmounted private data; after all repeats succeed, collect lifecycle logs and delete this run's shared fixture and remote deployment.

Protocol: warm shared data, no cache drop; prereading does not guarantee later cache residency. Private reads follow this round's create with no fsync/sync, so they do not prove durable-write latency. The mdtest wrapper, loop and barriers are inside upstream phase timing; the Python wrapper, MPI startup, deployment, fixture, warm-up and result output are outside it.

`--warningAsErrors` is enabled. Operation errors, wrong rank/node counts or actual placement, version/binary mismatch, and missing or nonfinite timings all fail; the repeat emits no successful throughput. A failure stops the run and leaves mounts and data in place for inspection: no automatic unmount, retry, failover, or shrinking to a smaller N. Inspect every machine before handling that run.

## Results and interpretation

The only raw result is still `results.jsonl`; a derived `summary.csv` is generated by default. Global workload cases look like `multi-mount/N1024/private/create`; per-host lifecycle/resource cases are under `.../hosts/<name>/`. `repeat` only distinguishes repetitions at the same N.

| Metric | Definition |
| --- | --- |
| `ops` | N×files |
| `elapsed_s` | Upstream rank CSV all-row phase time, including the phase-end barrier; not subprocess wall time |
| `ops_per_s` | N×files / elapsed_s; per-machine rates are never summed |
| `ops_per_s_per_client` | Aggregate throughput / N; not an independent per-client sample |
| `rank_mean_us_per_op` | sum(rank_elapsed_s)/(N×files)×1e6; phase mean, not per-operation latency/p99 |
| `barrier_wait_fraction` | 1 − mean(rank_elapsed_s)/elapsed_s: mean share of the phase a rank waits (spinning, in aggressive mode) for the slowest rank |
| `bytes`, `mib_per_s` | Logical create/read bytes; shared data is read N times, not physical network/disk bytes |
| Per-host `mount_elapsed_s`, `unmount_elapsed_s` | Wall time of all client lifecycle commands on that host; details keep per-client times and exclude alias/mountinfo verification |
| Global `controller_dispatch_elapsed_s` | From notifying all workers to receiving all replies, **including the control channel, scheduling and mount verification**; not pure mount time and not a sum of per-host times |
| `measured_mounts`, `remaining_mounts` | New/residual entries after per-host verification; global rows are sums, not inferred connection or daemon counts |
| Per-host `reserved_cpu_busy_cores`, `reserved_cpu_busy_percent` | Busy time on the reserved CPUs only. No rank runs there, so barrier spin is excluded; daemons, kernel threads, orted, the worker and other processes are included |

`scaling_efficiency` is no longer emitted: a single-point run has no same-round N=1 baseline. If a paper needs efficiency, choose a comparable baseline in separate runs; the harness does not add experiment points implicitly.

JSONL keeps the parsed N, requested_clients, K, workload, cache/fsync protocol, per-host available/assigned CPUs, hostname/kernel/architecture/tool versions, mdtest and profile SHA256, and the common source SHA256. `parameters.planned_rank_placement` is the planned rank→logical host→CPU map; `parameters.rank_placement` stores verified rank, logical host, hostname, CPU and affinity lists separately for each measured job such as `private` and `shared-1`. Every repeat re-aggregates proofs from all measured jobs; the last job cannot overwrite earlier records. K=1 has only `private`. These values are observed by the wrapper before exec, not continuously sampled affinity for the whole phase. SSH transport configuration and mount credentials are not written to JSONL; archive inventory/profile privately. Raw placement proofs, mdtest stdout/stderr, CSV/appfile remain as diagnostic attachments; analyzing actual placement does not depend on them or on the original machines.

Resources come from **whole-host counters on each machine**: CPU busy, MemTotal−MemAvailable, cache, dirty, slab, etc. `cpu_busy_*` includes ranks spinning in MPI barriers; use `reserved_cpu_busy_*` and `barrier_wait_fraction` to separate filesystem-side CPU from spin. The baseline/mounted-idle/released-idle samples record used/slab/unreclaimable/mount_entries deltas relative to baseline; the workload sample records observed peaks. Counters include Python, MPI, daemons, the kernel and other processes; there is no filesystem CPU/RSS attribution. Sampling windows include MPI startup/shutdown and phase gaps, and control timing differs across machines; do not add per-host peaks and call it a simultaneous global peak. Mount entries belong to the worker's current namespace, so a daemon must create mounts in that namespace.

## Mounted/active density

`--idle-clients-per-host M` (default 0, so no idle client is mounted unless requested) adds M mounted-only clients on every participating machine, with the same profile and K as the active clients: multi-mount adds M×K mount entries per machine, shared-directory adds M. Idle clients are mounted, verified and unmounted together with the active ones but never accessed: no rank, warm-up or sampler touches them, and they take no CPU slot. The active clients (one per usable CPU) run the unchanged workload. One run measures one M; cases are named `<topology>/N<N>-M<M>/...`, and an M=0 run with the same mode and N is the baseline. `mount_elapsed_s` and `measured_mounts` then include idle clients, and mounted-idle deltas show the per-machine cost of active + M mounted clients.

## Paper claim boundaries

- Keep per-client work fixed; only increasing N across runs constitutes weak scaling. In `remote` mode per-host density is fixed and only H changes; adding machines also adds client CPU, memory and network resources, so report placement and hardware and do not attribute all differences to mount count.
- Distinguish the same-backend `multi-mount` vs `shared-directory` ablation from cross-NAS comparisons; cross-system comparisons also mix server, cache, RPC and permission/consistency policy differences.
- Some measured systems may cap mounts at 1000; do not hardcode that as a capacity rule for all systems. With K=3, 333/334 agents correspond to 999/1002 **new experimental mount entries**; admin/other sessions may also consume provider quota. This round measures only successful-point performance, not rejection-boundary probing/recovery; do not claim "verified to reject here" from an unrun high N.
- By default targets run in order, without randomization; formal runs should interleave target order, retain repeat-sample dispersion, and check cache/slab history and background noise. Short directory/bind smokes are not paper performance data.
- No containers, models or real agents are started; there are no per-operation tail latency, durability or malicious-write isolation conclusions.

## Validation performed

Colima, non-root, Linux aarch64, Python 3.12.3, Open MPI 4.1.6, mdtest built from the official IOR 4.0.0 release:

| Scenario | Observed result |
| --- | --- |
| `--directory-smoke`, N=2, K=3, 32 files | 12 JSONL measurements; summary.csv generated |
| K=3 / K=1 directory runs after the placement fix | The K=3 JSONL contains hostname, CPU and single-core affinity for private/shared-1/shared-2; K=1 over two consecutive rounds contains only private, with no leftover shared proofs |
| `--directory-smoke` under `taskset` with only one logical CPU allowed | Fails explicitly requiring at least 2 available logical CPUs; starts no MPI and does not shrink implicitly |
| Default N, 6 available local logical CPUs, K=3 | Ranks pinned to CPUs 0–5; 18 dedicated Linux bind mounts created, mdtest completed, 0 residual after unmount |
| Explicit N=2, hosts file absent | Local only; full workload/lifecycle with 6 mounts succeeded |
| K=1, N=2, two repeats | Private create/stat/read/remove and two mount/unmount rounds succeeded |
| `taskset -c 1,3`, default N | Only two ranks, with actual affinity CPU 1 and 3; explicit N=3 rejected for insufficient capacity, no oversubscription |
| Real SSH worker from macOS→Colima | Package/profile distribution, preflight, 6 mounts, sampling, log collection, unmount with zero residue and deployment deletion succeeded; real SSH command execution through the inventory-aware MPI SSH agent succeeded |
| Repository unittest suite (after the reservation/remote/idle change) | `python -m unittest discover -s tests`: 125 tests OK, 2 skipped |
| `--directory-smoke` after the same change (Colima, 6 CPUs, smoke `reserved_cpus=1`) | Ranks on CPUs 0–1, CPU 5 reserved; every warm-shared-i and shared-i appfile carries `-R --random-seed=1`; jobs ran shared-1, shared-2, private in that order; 12 JSONL measurements including `barrier_wait_fraction` and `reserved_cpu_busy_*`. DuckDB was absent in the VM, so summary.csv was not generated |

Linux bind mounts **only validate the harness/lifecycle**, not independent NAS clients or storage performance. These temporary smoke results are not paper data. `remote` mode (MPI across independent hosts, remote rank-0 CSV fetch), idle clients, real NFS or LakeBase profiles, and thousand-client experiments have not been run; do not claim they passed performance or capacity validation.

## References

- [IOR 4.0.0 release](https://github.com/hpc/ior/releases/tag/4.0.0) and [mdtest native timing/CSV source](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) and [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
- [Linux NFS mount/cache semantics](https://man7.org/linux/man-pages/man5/nfs.5.html)
