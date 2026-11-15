# Mount scale: independent-client small-file I/O

Purpose: hold per-client small-file work fixed on a fixed compute-node budget and measure
whole-file read and create-write throughput as concurrent agents grow. The canonical paper
scenario uses one real backend mount and one private data set per agent. This is a proxy
workload for agent file operations, not an end-to-end agent benchmark.

Entry point: `agent_fs_bench/benchmarks/mount_scale.py`.
- `mount_scale_io.py` runs the synchronized read and create-write POSIX batches.
- `mount_scale_cluster.py` holds the persistent per-machine lifecycle workers.
- `mount_scale_mdtest.py` remains only for the explicitly selected K>1 shared-scope
  ablation; the canonical K=1 path does not execute mdtest.
- Placement, machine probing and MPI launch come from `agent_fs_bench/cluster.py`
  ([run modes](run-modes.md)).

## Experiment unit and placement

Each client is one MPI rank with its own real backend mount. With the default K=1
(`CONFIG['directories'] = 1`) each client has one private set containing:
- `read/`: 1,000 precreated 4 KiB files, prepared durably before the measured read;
- `write/`: an empty target for the measured create-write batch.

The paper scenario fixes **1,000 files × 4 KiB per client**. `files` and
`file_size_bytes` are recorded in `config.json`; formal runs keep them fixed across
systems and client counts.
Files contain seeded pseudorandom data, distinct across clients and files in
each phase. The 4 MiB per-client write payload is allocated before timing so
compression of zero-filled files and payload generation do not drive the result.

**One run measures one N on one fixed host budget; there is no default sweep.** Use
`--mode local` and increase N only across separate runs; do not add machines as N grows.
`CONFIG['tasks']` (default 64) selects N. N above the machine's CPU count oversubscribes
it, exactly as concurrent clients behave; the kernel schedules them. Do not treat added
machines as free concurrency or describe placement changes as an N-only scaling effect.

`directories > 1` selects the earlier shared read-only ablation. That path keeps its
upstream mdtest create/stat/read/remove private phases plus shared stat/read phases and is
reported under those phase names. It is not a paper default, and its shared phases are
never merged into the canonical `read` / `create-write` numbers.

Use one fixed machine for the intended sweep; increasing N does not add nodes. If a run
uses `--mode` with several machines anyway, placement is recorded per machine, but that
is no longer the canonical fixed-node-budget experiment and must not be presented as an
N-only scaling result.

## Dependencies and network

Every participating machine needs:
- Linux and Python 3.12+;
- this repository's checkout with `uv sync --locked`, at the same path by default;
- the same Open MPI 4.1.x version and architecture;
- GNU coreutils `sync` with `-f` (`syncfs`) for the create-write persistence boundary.

The K>1 shared-scope ablation additionally needs the **same upstream IOR 4.0.0 mdtest
binary on every machine**, linked against that Open MPI. The canonical K=1 path neither
builds nor validates mdtest.

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

For the K>1 ablation, build upstream IOR:

```sh
# Unpack the official ior-4.0.0.tar.gz and enter its source directory.
./configure CC=mpicc
make -j
# Install src/mdtest on each machine; prefer an absolute CONFIG['mdtest'], because
# Open MPI does not read shell profiles on remote machines.
```

## Configuration and runs

Experiment parameters live in `CONFIG` at the top of `mount_scale.py`; there are no
scaling or fault-tolerance CLI flags.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `tasks` | `64` | Single N per run, one client per rank |
| `profiles` | `local/mounts.toml` | Target mount profile, read on the controller |
| `directories` | `1` | K; default one private workspace and one mount per client; >1 selects the shared ablation |
| `files` | `1000` | Files per client in each canonical batch |
| `file_size_bytes` | `4096` | Bytes per file |
| `repeats` | `3` | Complete repetitions at the same N |
| `mdtest` | `mdtest` | Upstream binary used only by the K>1 ablation |
| `command_timeout_s` | `300` | Worker control replies and each mount/unmount command |
| `job_timeout_s` | `900` | Each native MPI job: read, create-write or ablation mdtest |
| `fixture_timeout_s` | `1800` | Per-repeat read-fixture preparation and cleanup control replies |
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
  sentinel and any K>1 shared resources.
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
| `{binds}` | Comma-separated volume-absolute `<shared source>:<agent shared-N>` mappings; K>1 ablation only |
| `{agent}` | Client number on the current machine, starting at 0 |

For K=1 each client must issue exactly one independent real backend mount. The CephFS
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
released. With K=1 the LakeBase profile must omit the empty `--bind` argument; the K>1
variant in the example file adds it. The canonical read fixture travels through the first
mount, so the read phase starts after an unmount/remount boundary.

```sh
uv run python -m agent_fs_bench run mount-scale --mode local --target cephfs
uv run python -m agent_fs_bench run mount-scale --mode local --target lakebase

# Two clients, ordinary directories, 32 files, one repeat; reads no profile and mounts nothing.
uv run python -m agent_fs_bench run mount-scale --mode local \
  --dirs /tmp/mdtest-directory-smoke -- --directory-smoke
```

`--directory-smoke` keeps ordinary data inside the staging work directory, so it runs on
one machine only: use `--mode local` (or a mode with one machine). Its two tasks are dealt
round-robin, so a mode with two or more machines spans them and the run fails before
starting workers/MPI. It exercises the read/create-write code without mounts and is not
storage-performance evidence.

## Execution and timing boundaries

1. Allocate N tasks over the selected machines from the cluster probe, and start one
   persistent lifecycle worker on each participating machine (over SSH for listed
   machines).
2. The first machine's worker creates the run's data root and owner sentinel. In each
   repeat, every machine creates its own private workspace and mountpoints and samples
   baseline; this preparation is outside I/O timing.
3. For K=1, mount the clients once, create the `read/` fixture through that first mount
   with exactly 1,000 full 4 KiB files, and call target-scoped `sync -f -- <read dir>` on
   every client. Create the separate empty `write/` target. Then unmount and mount again.
   The fixture preparation and both setup lifecycle steps are outside measured I/O time.
4. Sample mounted-idle, start per-machine resource sampling, and run the **read** MPI job:
   every agent opens, reads every byte of, and closes its 1,000 fixture files. The batch
   starts at an MPI barrier; `T` ends at the end barrier. No warm-up or cache drop is used,
   and read files are separate from the write target.
5. Run the **create-write** MPI job on the fresh `write/` target: every agent creates each
   file with `O_CREAT|O_EXCL`, writes exactly 4 KiB, and closes it. Each agent counts only
   exact full-file successes. After all agents finish, every agent checks all counts and
   then calls target-scoped `sync -f -- <write dir>`; the end barrier follows. `T` includes
   creation, writes, closes, the success check and those syncfs calls. There is no
   system-wide `sync`, no per-file `fsync`, and no direct I/O. `sync -f` selects the
   filesystem containing each client's target; when mounts share a superblock it may
   persist other clients' pending writes too.
6. Stop resource sampling, unmount concurrently in reverse order on each machine, verify
   no residue, and sample released-idle. On success, emit the repeat's results and delete
   the unmounted private data and empty repeat directory. After all repeats succeed,
   collect lifecycle logs and delete the run's data.

The canonical MPI jobs use `cluster.run_program` with `job_timeout_s` applied to the
launcher process group. A timeout, nonzero exit, short read/write count,
missing file, failed `sync -f`, or nonfinite timing fails the run. A failed or incomplete
batch emits **no** throughput row. Mounting, fixture preparation and cleanup never enter a
phase timer.

A failure stops the run and leaves mounts and data in place for inspection: no automatic
unmount, retry, failover or shrinking to a smaller N.

Read cache control is exclusively the unmount/remount after fixture preparation. The
benchmark does not call `drop_caches`, `posix_fadvise`, direct I/O or any other cache
invalidation. A remount is not proof of cold server, persistent, shared-superblock or
provider caches; report the mount type and options, and do not generalize beyond the
observed setup. Create-write latency is not a per-file durability latency: durability is
the final batch syncfs.

The K>1 ablation retains its earlier sequence: mount, mounted-idle, warm shared resources,
the global private mdtest create/stat/read/remove job, then per-resource shared jobs. Its
mount, fixture and mdtest timing boundaries are unchanged and its rows remain separate
from the canonical phases.

## Results and interpretation

Each run writes `results/mount-scale/<run-id>/config.json` (run configuration, mode,
machine facts and per-target discovered facts) plus one flat CSV per observation kind.
Figures come only from `plot`.

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

Canonical phase values are `read` and `create-write`. The K>1 ablation values remain
`private/create`, `private/stat`, `private/read`, `private/remove`, and
`shared-<n>/stat` / `shared-<n>/read`; do not treat a shared row as private work.

Column values:
- `action`: `mount` or `unmount`.
- `state`: in `resource_levels`, `baseline`, `mounted-idle` or `released-idle`;
  `resource_deltas` holds the last two.
- In `ranks`, `sample` is the global rank index and `host` is that task's machine.
- In `agents`, `sample` is the client index on that machine.

| Metric | Definition |
| --- | --- |
| `phases.ops` | Canonical: exact full files completed by all clients; ablation: N×files per mdtest phase |
| `phases.elapsed_s` | Canonical: T from the synchronized start through all completions and, for create-write, all target syncfs calls, using the slowest client; ablation: upstream mdtest all-row phase time including its end barrier |
| `phases.ops_per_s` | ops / elapsed_s; per-machine rates are never summed |
| `phases.ops_per_s_per_task` | Aggregate throughput / N; not an independent per-client sample |
| `phases.rank_mean_us_per_op` | sum(task times)/(ops)×1e6; phase mean, not per-operation latency/p99 |
| `bandwidth.bytes`, `bandwidth.mib_per_s` | Logical completed file bytes; read bytes are not physical network/disk bytes |
| `ranks.elapsed_s` | One task's start-to-end batch time |
| `host_mounts.elapsed_s`, `host_unmounts.elapsed_s` | Wall time of all client mount or unmount commands on that machine; excludes verification |
| `agents.elapsed_s` | One client's mount or unmount command sequence on that machine |
| `dispatch.controller_dispatch_elapsed_s` | From notifying all workers to receiving all replies, including control channel and mount verification; not a sum of per-machine times |
| `host_mounts.measured_mounts`, `host_unmounts.remaining_mounts` | New/residual entries after per-machine verification; mount entries are not inferred connection or daemon counts |

`scaling_efficiency` is not emitted: a single-N run has no same-round N=1 baseline. If a
paper needs efficiency, choose a comparable baseline in separate runs; the harness does
not add experiment points implicitly.

`config.json` stores the parsed configuration and `mode` once, the launcher's Open MPI
version (`mpi_version`), and each machine's probed facts. `targets[i].info` stores the
placement and, for K>1 only, the mdtest SHA-256 and version. The canonical K=1 path also
records `cache_control`, `completion` and `durability` facts.

`plot` writes the default figures; a figure whose table has no rows is skipped. Render
with `uv run python -m agent_fs_bench plot <run-dir>...`.

| Figure stem | Table | x | y | Split |
| --- | --- | --- | --- | --- |
| `throughput-<topology>-<phase>` | `phases` | tasks | ops_per_s | topology, phase |
| `mount-dispatch-<topology>-<action>` | `dispatch` | tasks | controller_dispatch_elapsed_s | topology, action |
| `memory-delta-<topology>` | `resource_deltas` | tasks | used_bytes_delta for `mounted-idle` | topology |

Each run measures one N, so its default figures hold one x point. Pool separate N runs
with `uv run python -m agent_fs_bench plot <run-N1> <run-N2>... --vary tasks`. All other
configuration, the mode and the source fingerprint must match; the fingerprint ignores
each module's `CONFIG` literal, so editing `CONFIG['tasks']` between runs is fine.

Resources come from **whole-machine counters on each machine**: CPU busy, MemTotal−
MemAvailable, cache, dirty, slab and mount entries. They include Python, MPI, daemons, the
kernel and other processes; there is no filesystem CPU/RSS attribution. Sampling windows
include MPI startup/shutdown and phase gaps. Mount entries belong to the worker's current
namespace, so a daemon must create mounts in that namespace.

## Paper claim boundaries

- The canonical design holds one fixed host budget and one fixed storage deployment.
  Adding N clients does not add nodes. Do not attribute a curve to node expansion.
- Each agent has one independent real backend mount and one private data set. A mount
  entry is not automatically a distinct NFS TCP connection, kernel client or cache; state
  the actual mount type/options and admin-view sharing. CephFS `noshare` is the example
  that creates a separate client instance per mount.
- Read and create-write are separate targets. The read fixture is durable before timing,
  and the read trial starts after a remount. Do not read back the write trial and call it
  a cold read, and do not claim per-file durability from the batch syncfs.
- No cache-drop or direct-I/O trick is used. Remounting does not guarantee cold persistent,
  server or provider caches.
- The default K=1 path emits no shared-phase rows. The K>1 shared-scope ablation is a
  separately selected layout experiment and must not be pooled into canonical private
  throughput.
- The workload is not an end-to-end agent benchmark: no models, containers or real agents
  run, and there are no tail-latency, malicious-write isolation or recovery claims.

## Validation performed

No post-change smoke run was performed while preparing this revision. The previous
implementation's directory and two-lab-server smokes validated its mount/lifecycle
harness but used the previous four-phase private workload; they are not evidence for the
read/create-write path. The directory smoke is still only a harness check, not storage
performance data.

## References

- [Linux `sync(1)` / syncfs](https://man7.org/linux/man-pages/man1/sync.1.html)
- [CephFS kernel mount and `noshare`](https://docs.ceph.com/en/reef/man/8/mount.ceph/)
- [Linux NFS mount/cache semantics](https://man7.org/linux/man-pages/man5/nfs.5.html)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) and
  [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [IOR 4.0.0 mdtest](https://github.com/hpc/ior/tree/4.0.0/src) for the K>1 ablation
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
