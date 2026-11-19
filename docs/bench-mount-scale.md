# Mount scale: independent clients and mount density

The [active paper matrix](../experiments/round-03.md) measures native mdtest file
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
`mdtest-io` uses **4 KiB per file**, and `mdtest-create` uses empty files.

The default `--workload mdtest-io` runs one native mdtest job per repeat on the
freshly mounted clients: create and write 4 KiB with a per-file `fsync`, then read
the same files in the same job. There is no fixture phase, remount or cache drop
between create and read. `mdtest-create` runs zero-byte create and close only;
`mount` runs one site mount command per active rank and no file I/O. The run
snapshot records the workload, file count and size; hold them fixed across a
comparison. mdtest removes its test trees in the job after the reported phases.

**One run measures one N; there is no implicit sweep.** Set N with `--tasks`
(default 64). Use `--mode remote` with the same ordered client pool and storage
deployment for a multi-host curve, and `--mode local` for density. Record actual
rank placement. The main three-host curve uses all three hosts at its planned
N > 1 points. N=1 uses only the first host and is a single-host reference.
N above the CPU count oversubscribes the hosts; keep different host budgets separate.

## Dependencies and network

Every participating machine needs:
- Linux and Python 3.12+;
- this repository's checkout with `uv sync --locked`, at the same path by default;
- the same Open MPI 4.1.x version and architecture;
- For `mdtest-create` or `mdtest-io`: the same upstream
  IOR 4.0.0 `mdtest`, linked against that Open MPI. `mount` needs no mdtest binary.

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

## Configuration and runs

Pass `--workload`, `--idle-mounts`, `--tasks`, `--repeats`, `--startup-timeout-s`,
`--job-timeout-s` and `--fixture-timeout-s` after `--`. Other parameters live in
`CONFIG` at the top of `mount_scale.py`.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `tasks` | `64` | Active mounts, one rank per client |
| `workload` | `mdtest-io` | Native create+read; `mdtest-create` and `mount` are selected explicitly |
| `idle_mounts` | `0` | Extra mounts, no ranks; one participating host, `mount` or `mdtest-create` only |
| `profiles` | `local/mounts.toml` | Target mount profile, read on the controller |
| `files` | `1000` | Files per active client in an mdtest batch |
| `file_size_bytes` | `4096` | Bytes per `mdtest-io` file; `mdtest-create` always uses 0 B |
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
example uses the kernel client's `noshare` option, which creates a new client instance per
mount instead of sharing one instance and its caches among mounts of the same cluster.
Configure the actual Ceph monitors, filesystem name, client name and secret file; no
profile in this repository is a claim about a real deployment.

For NFS, one mount entry is not necessarily one independent NFS TCP connection, client
instance or cache: Linux may share a superblock among mounts of the same server/export.
The example keeps normal cache defaults and does not silently add `nosharecache`; report
the actual mount options and connection behavior with the results. A FUSE profile must
ensure each mount creates a real filesystem client; one mount plus a Linux bind cannot
stand in for multiple clients.

Mount commands must return after the mount is established and unmount commands after it is
released. The LakeBase profile must omit the empty `--bind` argument.

The harness has no cgroup setting and creates no budget cgroup. Experiments 1–4 require
the site to apply an external common 1 CPU / 2 GiB budget around each workload and its
FUSE daemon where present; experiments 5–8 use fixed host resources without a per-agent
quota. Upstream mdtest itself has no built-in CPU or memory limit.

```sh
uv run python -m agent_fs_bench run mount-scale --mode remote --target cephfs -- \
  --workload mdtest-io --tasks 384 --repeats 3
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mount --tasks 1 --idle-mounts 64 --repeats 100
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mdtest-create --tasks 1 --idle-mounts 64 --repeats 100
```

## Execution and timing boundaries

For `mount`, each active MPI rank crosses a start barrier, then runs one site
mount wrapper. The latency is from invocation to its ready return, not MPI
startup, controller dispatch, or the post-return mount and path verification.
No file workload runs. Do not insert artificial sleeps or a stagger inside the
timed wrapper. The worker verifies and unmounts all active mounts in every repeat.

`mdtest-create` records only `file-create`: zero-byte create and close, without
write or `fsync`. Cleanup does not contribute to create time. `mdtest-io`
records `create-write` and `read` from the same native job: create and write
with `-y` per-file `fsync`, then read without remount or cache eviction. The
file `fsync` is not a separate directory sync. Both modes use native aggregate
phase times and check the work count and rank completion. Ignore neither a
short phase nor a warning about failed I/O.

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
   mounted-idle and start per-machine resource sampling. Then run one global native mdtest
   job with one rank per active client:
   - `mdtest-create` runs zero-byte create and close only (`-C -w 0`).
   - `mdtest-io` creates and writes 4 KiB with `-y` per-file `fsync`, then reads the same
     file set in the same job; there is no remount, fixture or cache drop between phases.
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
Figures come only from `plot`. Every active observation row carries `workload` and
`idle_mounts`; `idle_mounts`/`idle_unmounts` rows carry `workload`.

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | target, topology, tasks, phase, repeat | ops, elapsed_s, ops_per_s, ops_per_s_per_task, rank_mean_us_per_op |
| `bandwidth` | target, topology, tasks, phase, repeat | bytes, elapsed_s, mib_per_s |
| `ranks` | target, topology, tasks, phase, repeat, sample, host | elapsed_s |
| `dispatch` | target, topology, tasks, action, repeat | controller_dispatch_elapsed_s |
| `host_mounts` | target, topology, tasks, host, repeat | elapsed_s, measured_mounts |
| `host_unmounts` | target, topology, tasks, host, repeat | elapsed_s, remaining_mounts |
| `agents` | target, topology, tasks, host, action, repeat, sample | elapsed_s |
| `resource_levels` | target, topology, tasks, host, state, repeat | raw levels and sampling interval |
| `resource_deltas` | target, topology, tasks, host, state, repeat | used_bytes_delta, slab_bytes_delta, slab_unreclaimable_bytes_delta, mount_entries_delta |
| `resource_workload` | target, topology, tasks, host, repeat | sampling interval, peak_* and peak_*_delta |
| `idle_mounts` / `idle_unmounts` | target, topology, tasks, idle_clients, host | Lifecycle elapsed time and measured/residual mount counts for the idle cohort |

Native phase values are `file-create` or `create-write` and `read` according to
the workload. `mount` emits no I/O or resource workload rows. Keep results separated by
`config.workload`, `config.idle_mounts` and the mount profile.

Column values:
- `action`: `mount` or `unmount`.
- `state`: in `resource_levels`, `baseline`, `mounted-idle` or `released-idle`;
  `resource_deltas` holds the last two.
- In `ranks`, `sample` is the global rank index and `host` is that task's machine.
- In `agents`, `sample` is the client index on that machine.

| Metric | Definition |
| --- | --- |
| `phases.ops` | N×files for the validated mdtest phase |
| `phases.elapsed_s` | The mdtest all-row phase time including the end barrier |
| `phases.ops_per_s` | ops / elapsed_s; per-machine rates are never summed |
| `phases.ops_per_s_per_task` | Aggregate throughput / N; not an independent per-client sample |
| `phases.rank_mean_us_per_op` | sum(task times)/(ops)×1e6; phase mean, not per-operation latency/p99 |
| `bandwidth.bytes`, `bandwidth.mib_per_s` | Logical completed file bytes; read bytes are not physical network/disk bytes and may be client-cache hits |
| `ranks.elapsed_s` | One rank's mdtest phase time before the end barrier; not a single file latency |
| `host_mounts.elapsed_s`, `host_unmounts.elapsed_s` | Worker-launched commands: host wall time excluding verification. `--workload mount` mounts instead report the longest active rank's wrapper duration on that host; use `agents` for the distribution. |
| `agents.elapsed_s` | One client's mount or unmount command sequence on that machine |
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
revision. A minimal real-backend smoke remains required.

## References

- [CephFS kernel mount and `noshare`](https://docs.ceph.com/en/reef/man/8/mount.ceph/)
- [Linux NFS mount/cache semantics](https://man7.org/linux/man-pages/man5/nfs.5.html)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) and
  [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [IOR 4.0.0 mdtest](https://github.com/hpc/ior/tree/4.0.0/src)
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
