# Mount scale: independent clients and mount density

The [active paper matrix](../experiments/round-04.md) measures native mdtest file
throughput, mount-command latency, and the effect of idle and active mounts on one
client host. Each active task has its own real backend mount and private directory.

Entry point: `agent_fs_bench/benchmarks/mount_scale.py`.
- `mount_scale_mdtest.py` runs native IOR 4.0.0 through real client mounts.
- `mount_scale_cluster.py` holds the persistent per-machine lifecycle workers.
- `mount_scale_mounts.py` parses the private profile and runs mount/unmount commands.
- Placement, machine probing and MPI launch come from `agent_fs_bench/cluster.py`
  ([run modes](run-modes.md)).

## Experiment unit and placement

Each active client is one MPI rank with its own mount. `--idle-mounts M` adds M
real mounts on the same host without adding ranks; they remain mounted across the
point's active repeats. The active batches use **1,000 files per rank**;
`mdtest-io` uses **4 KiB per file**, and `mdtest-create` uses
`--create-file-size-bytes` (default `0`; the create-only paper axis uses 4 KiB).

The default `--workload mdtest-io` runs one native mdtest job per repeat on the
freshly mounted clients: create and write 4 KiB with a per-file `fsync`, then read
the same files in the same job. There is no fixture phase, remount or cache drop
between create and read. `mdtest-create` runs create, optional write and close
only, without a per-file `fsync`; `--stonewall-seconds` is an optional native
create-only deadline. `mount` runs one site mount command per active rank and no
file I/O. The run snapshot records the workload, file count and size; hold them
fixed across a comparison. mdtest removes its test trees in the job after the
reported phases.

The three file workloads are never mixed: `mdtest-create` at 4 KiB is
create+write+close without `-y`; density `mdtest-create` points use the native
0 B default; `mdtest-io` is 4 KiB create+write with `-y` followed by read in the
same job.

**One run measures one N; there is no implicit sweep.** Set N with `--tasks`
(default 64). Use `--mode remote` with the same ordered client pool and storage
deployment for a multi-host curve, and `--mode local` for density. Record actual
rank placement. The Round 04 main curve uses the frozen ten-client pool at every
planned N > 1 point, not only a subset of hosts. N=1 uses only the first host and is
a single-host reference.
N above the CPU count oversubscribes the hosts; keep different host budgets separate.

## Dependencies and network

Every participating machine needs:
- Linux and Python 3.12+;
- this repository's checkout with `uv sync --locked`, at the same path by default;
- the same Open MPI 4.1.x version and architecture;
- For `mdtest-create` or `mdtest-io`: the same unmodified upstream
  IOR 4.0.0 `mdtest`, linked against that Open MPI, with one recorded SHA-256 per
  machine. `mount` needs no mdtest binary.

Open MPI requires:
1. Non-interactive SSH from the controller to each worker, with host keys trusted.
2. Bidirectional MPI TCP reachability between participating machines and resolvable
   hostnames/addresses.
3. Open MPI startup, barriers and task communication do not use a benchmark-built SSH
   tunnel. Opening TCP 22 alone is not enough. Force plain TCP and same-host shared
   memory; RDMA is not required.
4. On multi-NIC machines, select mutually reachable MPI interfaces explicitly in
   `CONFIG['mpiexec_args']` of `agent_fs_bench/cluster.py`, and archive it.

The controller also needs the Arial font for `plot`; a missing font fails only figure
rendering, not a run. The benchmark installs no tools and uses no sudo.

For native mdtest workloads, build upstream IOR:

```sh
# Unpack the official ior-4.0.0.tar.gz and enter its source directory.
./configure CC=mpicc
make -j
# Install src/mdtest on each machine; prefer an absolute CONFIG['mdtest'], because
# Open MPI does not read shell profiles on remote machines.
```

The unmodified IOR 4.0.0 source commit is
`967a9f65109760db8a3ac14a7fdd007f337d2960`. Formal hosts choose one build and verify
the same binary identity on every machine; a different build or MPI version is not
the same group.

## Configuration and runs

Pass `--workload`, `--idle-mounts`, `--create-file-size-bytes`,
`--stonewall-seconds`, `--tasks`, `--repeats`, `--startup-timeout-s`,
`--job-timeout-s` and `--fixture-timeout-s` after `--`. Other parameters live in
`CONFIG` at the top of `mount_scale.py`. The two create options are accepted only
with `--workload mdtest-create`; any other workload rejects them instead of
silently ignoring them.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `tasks` | `64` | Active mounts, one rank per client |
| `workload` | `mdtest-io` | Native create+read; `mdtest-create` and `mount` are selected explicitly |
| `idle_mounts` | `0` | Extra mounts, no ranks; one participating host, `mount` or `mdtest-create` only |
| `profiles` | `local/mounts.toml` | Target mount profile, read on the controller |
| `files` | `1000` | Files per active client in an mdtest batch |
| `create_file_size_bytes` | `0` | Bytes per `mdtest-create` file; a positive value includes write and close, never `-y` |
| `stonewall_seconds` | `None` | Positive native `-W` seconds for `mdtest-create` only; `None` omits `-W` |
| `file_size_bytes` | `4096` | Bytes per `mdtest-io` file; ignored by `mdtest-create` |
| `repeats` | `3` | Complete repetitions at the same N |
| `mdtest` | `mdtest` | Upstream binary for native phases |
| `command_timeout_s` | `360` | Each mount/unmount command and short worker replies |
| `lifecycle_timeout_s` | `900` | Each worker's full mount/unmount action, including verification |
| `startup_timeout_s` | `180` | Deadline for every MPI rank to report its rank and planned host before `MPI_Init` |
| `job_timeout_s` | `900` | Whole MPI job, including native mdtest or the active mount wrappers |
| `fixture_timeout_s` | `1800` | Per-repeat cleanup, `remove-repeat` and `remove-data` control replies |
| `idle_sample_s` | `1.0` | Machine idle sampling duration for baseline/mounted/released |
| `resource_sample_s` | `0.1` | Machine resource sampling period during the workload |

1000 files of 4 KiB are a normal paper default, but formal runs still need phase durations
well above MPI barrier and timer overhead. Do not claim peak throughput from a short
smoke.

### Target and mount profile

The target is a **local staging root at the same absolute path on every machine**. It must
already exist and be outside the admin volume. Results go to
`results/mount-scale/<run-id>/` in the controller repository. Each machine's lifecycle
worker runs from its own checkout and works under
`<target>/agent-fs-bench/mount-scale/<run-id>/<node>/`.

Copy and edit `experiments/mounts.example.toml` to `local/mounts.toml` on the controller.
The controller sends this private profile as text to each worker over the job's stdin; all
participating machines use the same target profile. External tools, mount permissions and
admin mounts are provisioned by the experiment environment.

`admin_root` must be pre-mounted at the same path on every machine as a **full admin view
of the same volume**. Prepared data lives under
`<admin_root>/agent-fs-bench/mount-scale/<run-id>/`:
- The first participating machine's worker creates it, with this run's random owner
  sentinel.
- Every worker reads the sentinel before setup and rejects a disconnected admin namespace.
- Per-repeat private data is separated by logical machine name; agent numbering starts at
  0 on each machine.

Mount profile argv is expanded with `format_map`, without a shell, using these
placeholders:

| Placeholder | Content |
| --- | --- |
| `{source}` | Volume-internal path starting with `/`, relative to the admin volume root |
| `{mountpoint}` | Actual mountpoint on the current machine |
| `{subdir}` | Private volume-internal path; workspace for multi-mount, agent root for shared-directory |
| `{agent}` | Client number on the current machine, starting at 0 |

Each client must issue exactly one independent real backend mount. The CephFS
formal example starts one real `ceph-fuse` daemon per rank from a site wrapper; the
kernel client's `noshare` profile in the example file is historical/illustrative, not the
Round 04 formal CephFS arm. Configure the actual Ceph monitors, filesystem name, client
name and secret file; no profile in this repository is a claim about a real deployment.

For NFS, one mount entry is not necessarily one independent NFS TCP connection, client
instance or cache: Linux may share a superblock among mounts of the same server/export.
The example keeps normal cache defaults and does not silently add `nosharecache`; report
the actual mount options and connection behavior with the results. A FUSE profile must
ensure each mount creates a real filesystem client; one mount plus a Linux bind cannot
stand in for multiple clients.

Mount commands must return after the mount is established and unmount commands after it is
released. The LakeBase profile must omit the empty `--bind` argument.

### Site budget hook (`rank_cgroup`)

Mount-scale creates no quota and removes nothing from a cgroup. A profile may declare an
optional scalar string:

```toml
rank_cgroup = "/sys/fs/cgroup/agent-fs-bench/{mount_hash}"
```

`{mount_hash}` is `sha256(str(actual_mountpoint).encode()).hexdigest()[:20]`. The template
must be absolute, strictly under `/sys/fs/cgroup`, and contain exactly one `{mount_hash}`
placeholder and no other placeholder or format spec. `cgroup = true`, `mount_cgroup` and
the retired quota layouts remain unknown profile keys; there is no alias and no fallback
to the old implementation. This is a thin site-owned path hook, not a generic limiter.

The site mount wrapper owns the group and its limits:

- It creates the group for each actual mountpoint before starting a FUSE daemon, records
  the known daemon PID, and reads `/proc/<daemon_pid>/cgroup` to verify that exact PID is
  in the group while `cpu.max = "100000 100000"` and `memory.max = 2147483648` are in
  effect. The workload rank later joins the same group; daemon, rank and children share
  one 1c2g budget.
- For NFS the wrapper still creates the group, but only the rank joins it. Kernel NFS
  service threads are not constrained; report that explicitly instead of calling the
  group a kernel-thread or server quota. Do not claim a symmetric total kernel/system
  budget across systems.
- A normal unmount wrapper performs the real unmount, waits for the group to become
  empty and removes it, returning nonzero if processes or the group remain. The harness
  never creates, resizes or removes groups.
- On harness failure mounts and data stay in place: there is no automatic unmount,
  cleanup, retry or failed-mount rescue.

The site must delegate the cgroup v2 `cpu` and `memory` controllers. A user service with
only `memory`/`pids` delegation cannot establish or prove the fixed 1c2g budget, so
budgeted formal points are `not_run`; a memory-only group must not be reported as 1c2g.
This repository's smoke verification does not claim a successful CPU-limited join when
the site lacks that delegation.

A budgeted native rank starts through
`python -u -m agent_fs_bench.benchmarks.mount_scale_runner mdtest --cgroup <path> -- ...`.
The runner joins the group and emits exactly one line per configured rank:

```text
MDBENCH_PLACEMENT {"cgroup": "...", "pid": <int>, "rank": <int>, "cpu_max": "100000 100000", "memory_max": 2147483648, "members": [<int>, ...]}
```

The harness rejects a missing or duplicate proof, wrong rank, wrong cgroup or limits, or
a rank PID absent from `members`. `members` lists observed PIDs only; it does not by
itself identify a FUSE daemon, so daemon identity remains the site wrapper's recorded
PID plus the `/proc/<pid>/cgroup` check. Raw proof stays in the run's native diagnostics
and is not uploaded. Timed mount mode (`--workload mount`) rejects `rank_cgroup`:
mount-latency profiles are unbudgeted and must not insert a budgeted group into the timed
wrapper; the density profiles use the same fixed-host, unbudgeted rule.

The lifecycle worker returns a `budgets` list in client order; it becomes one
`budget_events` row per active mount or unmount client. The mount snapshot is taken after
the wrapper establishes the group; the unmount snapshot before the wrapper removes it.
The counters are `cpu_nr_periods`, `cpu_nr_throttled`, `cpu_throttled_usec`,
`memory_max_events`, `memory_oom` and `memory_oom_kill`; record them, do not raise the
quota to rescue a point. Tools, peak and replay use their own site external
whole-tree budget, not this mount-scale hook. Clone keeps its separate fixed-resource
contract and does not inherit the 1c2g budget.

```sh
uv run python -m agent_fs_bench run mount-scale --mode remote --target cephfs-fuse -- \
  --workload mdtest-io --tasks 384 --repeats 3
uv run python -m agent_fs_bench run mount-scale --mode remote --target lakebase-fast-drop -- \
  --workload mdtest-create --create-file-size-bytes 4096 --stonewall-seconds 600 \
  --tasks 1000 --repeats 1
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mount --tasks 1 --idle-mounts 64 --repeats 100
```

## Execution and timing boundaries

For `mount`, each active MPI rank crosses a start barrier, then runs one site
mount wrapper. The latency is from invocation to its ready return, not MPI
startup, controller dispatch, or the post-return mount and path verification.
No file workload runs. Do not insert artificial sleeps or a stagger inside the
timed wrapper. The worker verifies and unmounts all active mounts in every repeat.

`mdtest-create` records only `file-create`: create, optional write and close,
without a per-file `fsync` (the default 0 B writes no data; 4 KiB create-only
writes data but still does not fsync). Cleanup does not contribute to create
time. `--stonewall-seconds` passes native `-W`; a complete phase may report
`elapsed_s` above W because native stonewall catches slow ranks to the maximum
completed rank, and the aggregate row includes that maximum plus the end barrier.
Wall above W is not truncation by itself. `mdtest-io` records
`create-write` and `read` from the same native job: create and write with `-y`
per-file `fsync`, then read without remount or cache eviction. The file `fsync`
is not a separate directory sync. Both modes use native aggregate phase times and
check the work count and rank completion. Ignore neither a short phase nor a
warning about failed I/O.

### Completion count and raw proof

For every phase, the harness reconstructs the completed operation count from the
native rank CSV's `rate × time`; IOR 4.0.0 prints rate and time with 11 significant
digits, so the product must round to an unambiguous positive integer within that
error bound. It rejects ambiguous or non-integer products, then requires
`aggregate == sum(rank counts)` and every rank count `== files`, so the total is
`N × files`. A truncated stonewall job fails here even if mdtest reported a phase
time. This follows unmodified IOR 4.0.0's
[rate/time rows](https://github.com/hpc/ior/blob/967a9f65109760db8a3ac14a7fdd007f337d2960/src/mdtest.c#L1118-L1141)
and
[completed-item output](https://github.com/hpc/ior/blob/967a9f65109760db8a3ac14a7fdd007f337d2960/src/mdtest.c#L1425-L1463). The fetched native rank CSV is written to the job's
`diagnostics/rank-performance.csv` before parsing and validation, preserving the
raw truncated proof on the experiment controller. That file is not part of a
summary exchange.

When configured, the worker creates and verifies the idle cohort **before** any
active repeat and releases it at the end. Idle clients contribute no active
mount samples or file operations. Use a complete `idle_unmounts` record and
the actual verified idle count before accepting density results.

### Native mdtest sequence (`mdtest-create` / `mdtest-io`)

1. Allocate N tasks over the selected machines from the cluster probe, and start one
   persistent lifecycle worker on each participating machine (over SSH for listed
   machines).
2. The first machine's worker creates the run's data root and owner sentinel. In each
   repeat, every machine creates its own private workspace and mountpoints and samples
   baseline; this preparation is outside phase timing.
3. Mount the active clients concurrently on every machine, verify the mounts, sample
   mounted-idle and start per-machine resource sampling. A profile with `rank_cgroup`
   also verifies its site-created groups. Then run one global native mdtest job with one
   rank per active client:
   - `mdtest-create` runs create+write+close without `-y`
     (`-C -w <create_file_size_bytes> -e <same>`; optional `-W <stonewall_seconds>`).
     The default 0 B is the density/legacy create shape; the create-only paper axis
     passes 4096 and 600.
   - `mdtest-io` creates and writes 4 KiB with `-y` per-file `fsync`, then reads the same
     file set in the same job; there is no remount, fixture or cache drop between phases.
     It does not accept the create-size or stonewall options.
   The reported phase starts at mdtest's own barrier and ends at its end barrier; the
   untimed `-r` removal runs before job exit.
4. Stop resource sampling, unmount concurrently on each machine, verify
   no residue, sample released-idle, and delete the unmounted private data. Only then emit
   the repeat's results. After all repeats succeed, collect lifecycle logs and delete the
   run's data.

The launcher checks a startup report from each planned rank before `MPI_Init`. A missed
startup deadline fails with the missing rank count per host; a duplicate or misplaced
rank fails immediately. This is separate from the full `job_timeout_s` deadline, which
also bounds hangs after all ranks have spawned. Both deadlines terminate the local MPI
launcher process group, retaining stdout and stderr. A timeout, nonzero exit, unexpected
mdtest warning, missing rank, wrong item count, or nonfinite timing fails the repeat
without emitting throughput. Mount commands are measured separately; mounting, cleanup
and untimed mdtest removal are outside phase timing.

A failure stops the run and leaves mounts and data in place for inspection: no automatic
unmount, retry, failover or shrinking to a smaller N.

Read cache control is deliberately absent: the native read phase follows the create phase
in the same mdtest launch, and the benchmark does not call `drop_caches`,
`posix_fadvise`, direct I/O or any other cache invalidation. This is not proof of cold
server, persistent, shared-superblock or provider caches; report the mount type and
options, and do not generalize beyond the observed setup. `-y` fsyncs each file; mdtest
does not separately fsync directory entries, so these numbers are not a crash-durability
claim.

## Results and interpretation

Each run writes `results/mount-scale/<run-id>/config.json` (run configuration, mode,
machine facts and per-target discovered facts) plus one flat CSV per observation kind.
Figures come only from `plot`. Every active observation row carries `workload`,
`idle_mounts`, the actual `file_size_bytes` (0 for `mount`), `stonewall_seconds` (blank
when unused), `per_file_fsync` (0/1) and `budget` (`1c2g`/`none`), as well as the usual
target, topology, tasks and repeat keys. `idle_mounts`/`idle_unmounts` rows carry
`workload`.

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | target, topology, tasks, phase, repeat | ops, elapsed_s, ops_per_s, ops_per_s_per_task, rank_mean_us_per_op |
| `bandwidth` | target, topology, tasks, phase, repeat | bytes, elapsed_s, mib_per_s |
| `ranks` | target, topology, tasks, phase, repeat, sample, host | elapsed_s |
| `dispatch` | target, topology, tasks, action, repeat | controller_dispatch_elapsed_s |
| `host_mounts` | target, topology, tasks, host, repeat | elapsed_s, measured_mounts |
| `host_unmounts` | target, topology, tasks, host, repeat | elapsed_s, remaining_mounts |
| `agents` | target, topology, tasks, host, action, repeat, sample | elapsed_s |
| `budget_events` | target, topology, tasks, host, action, repeat, sample, cgroup | cpu_nr_periods, cpu_nr_throttled, cpu_throttled_usec, memory_max_events, memory_oom, memory_oom_kill |
| `resource_levels` | target, topology, tasks, host, state, repeat | raw levels and sampling interval |
| `resource_deltas` | target, topology, tasks, host, state, repeat | used_bytes_delta, slab_bytes_delta, slab_unreclaimable_bytes_delta, mount_entries_delta |
| `resource_workload` | target, topology, tasks, host, repeat | sampling interval, peak_* and peak_*_delta |
| `idle_mounts` / `idle_unmounts` | target, topology, tasks, idle_clients, host | Lifecycle elapsed time and measured/residual mount counts for the idle cohort |

Native phase values are `file-create` or `create-write` and `read` according to
the workload. `mount` emits no I/O or resource workload rows. Keep results separated by
`config.workload`, `config.idle_mounts`, the actual file size/stonewall and fsync flag,
`budget`, and the mount profile. `budget_events` rows exist only for profiles with
`rank_cgroup`; `cgroup` is the site-created path and `sample` is the client index on that
host, matching `agents`.

Column values:
- `action`: `mount` or `unmount`.
- `state`: in `resource_levels`, `baseline`, `mounted-idle` or `released-idle`;
  `resource_deltas` holds the last two.
- In `ranks`, `sample` is the global rank index and `host` is that task's machine.
- In `agents`, `sample` is the client index on that machine.

| Metric | Definition |
| --- | --- |
| `phases.ops` | Completed operations reconstructed from native `rate × time`, checked as N×files and as the sum of per-rank counts |
| `phases.elapsed_s` | The mdtest all-row phase time including the end barrier |
| `phases.ops_per_s` | ops / elapsed_s; per-machine rates are never summed |
| `phases.ops_per_s_per_task` | Aggregate throughput / N; not an independent per-client sample |
| `phases.rank_mean_us_per_op` | sum(task times)/(ops)×1e6; phase mean, not per-operation latency/p99 |
| `bandwidth.bytes`, `bandwidth.mib_per_s` | Logical completed file bytes; read bytes are not physical network/disk bytes and may be client-cache hits |
| `ranks.elapsed_s` | One rank's mdtest phase time before the end barrier; not a single file latency |
| `host_mounts.elapsed_s`, `host_unmounts.elapsed_s` | Worker-launched commands: host wall time excluding verification. `--workload mount` mounts instead report the longest active rank's wrapper duration on that host; use `agents` for the distribution. |
| `agents.elapsed_s` | One client's mount or unmount command sequence on that machine |
| `budget_events.*` | Kernel counters of one site cgroup at a mount or unmount snapshot; mount after group creation, unmount before group removal. They prove placement/limits and record OOM/throttling, not per-daemon CPU or RSS attribution. |
| `dispatch.controller_dispatch_elapsed_s` | From notifying all workers to receiving all replies, including control channel and mount verification; not a sum of per-machine times |
| `host_mounts.measured_mounts`, `host_unmounts.remaining_mounts` | New/residual entries after per-machine verification; mount entries are not inferred connection or daemon counts |

`scaling_efficiency` is not emitted: a single-N run has no same-round N=1 baseline. If a
paper needs efficiency, choose a comparable baseline in separate runs; the harness does
not add experiment points implicitly.

`config.json` stores the parsed configuration and `mode` once, the launcher's Open MPI
version (`mpi_version`), and each machine's probed facts. `targets[i].info` stores
placement, native mdtest binary identity as applicable, and the completion/durability
boundary: mount-ready for `mount`, and mdtest's phase end for native modes, with `-y`
per-file fsync and no directory fsync for `mdtest-io`.

`plot` writes the default figures; a figure whose table has no rows is skipped. Render
with `uv run python -m agent_fs_bench plot <run-dir>...`.

| Figure stem | Table | x | y | Split |
| --- | --- | --- | --- | --- |
| `throughput-<topology>-<phase>` | `phases` | tasks | ops_per_s | topology, phase |
| `mount-dispatch-<topology>-<action>` | `dispatch` | tasks | controller_dispatch_elapsed_s | topology, action |
| `memory-delta-<topology>` | `resource_deltas` | tasks | used_bytes_delta for `mounted-idle` | topology |

Each run measures one N, so its default figures hold one x point. Pool separate N runs
with `uv run python -m agent_fs_bench plot <run-N1> <run-N2>... --vary tasks`. Keep
all other measurement settings, the mode and the source fingerprint fixed.

Resources come from **whole-machine counters on each machine**: CPU busy, MemTotal−
MemAvailable, cache, dirty, slab and mount entries. They include Python, MPI, daemons, the
kernel and other processes; there is no filesystem CPU/RSS attribution. Sampling windows
include MPI startup/shutdown and phase gaps. For native mdtest runs the sampling window
also covers the job's untimed `-r` delete phase, so `resource_workload` peaks are not a
create-only or read-only attribution. Mount entries belong to the worker's current
namespace, so a daemon must create mounts in that namespace.

## Paper claim boundaries

- Keep the client pool and storage deployment fixed for a multi-host curve.
  The N=1 reference uses one participating host; do not treat it as a
  same-budget baseline for scaling efficiency.
- Each agent has one independent real backend mount and one private data set. A mount
  entry is not automatically a distinct NFS TCP connection, kernel client or cache; state
  the actual mount type/options and admin-view sharing. CephFS `noshare` is the example
  that creates a separate client instance per mount.
- State the protocol: the native read phase follows creation in the same launch;
  `mdtest-io` uses per-file `fsync` and no directory fsync. Neither establishes cold
  provider caches nor crash durability.
- Every create-only/mdtest-io scale-axis rank has the same 1c2g budget when
  `rank_cgroup` is set; a FUSE daemon shares that group. The NFS profile's group
  contains only the rank, and kernel NFS threads are not constrained; do not describe
  it as a server quota or a symmetric total system budget. Timed mount and density
  profiles are fixed-host and unbudgeted by design.
- `mdtest-create` at 4 KiB is create+write+close without `-y`; density points use the
  native 0 B default; `mdtest-io` is 4 KiB create+fsync+read. These axes never share a
  series or a throughput comparison.
- A N ≥ 3200 LakeBase point with `max_open_conns=4&max_idle_conns=4` is a deployment
  variant, not an independent cache factor; label it as a variant and do not splice it
  into a single-factor ablation.
- For mount p50/p99 use the active `agents.csv` command durations. For density
  create p50/p99 use the active `ranks.csv` create-batch durations. Give both the
  number of commands or ranks and the number of measured batches; never
  describe either as per-file tail latency.
- These workloads do not establish end-to-end agent latency, crash durability,
  or isolation correctness.

## Previous smoke observations

These are historical observations from earlier revisions, not tests of this one. An
earlier revision included a `--directory-smoke` mode (now removed); on Linux with
Open MPI 4.1.8 it started both ranks and completed 64 files in each of its read and
create-write batches. It never tested a real backend mount.

Round 03 exercised `mount`, `mdtest-create`, and `mdtest-io` with real Linux bind
mounts in a private namespace, Open MPI 4.1.6, and IOR 4.0.0. `mdtest-io` used two
active ranks and completed 64 files per phase. `mount` and `mdtest-create` each
used one active rank and three idle mounts. All mounts and data were released.
These observations validate the old lifecycle and result counts; they were not run
against an independent FUSE/NFS backend and do not establish this native-only
revision. A Linux bind-mount lifecycle smoke (including the new create flags) can
validate argv, counts and cleanup, but it is neither a real FUSE/NFS backend test
nor evidence of a successful 1c2g cgroup join. A minimal real-backend and real-site
budget smoke remains required before formal data.

## References

- [CephFS kernel mount and `noshare`](https://docs.ceph.com/en/reef/man/8/mount.ceph/)
- [Linux NFS mount/cache semantics](https://man7.org/linux/man-pages/man5/nfs.5.html)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) and
  [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [IOR 4.0.0 mdtest](https://github.com/hpc/ior/tree/4.0.0/src)
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
