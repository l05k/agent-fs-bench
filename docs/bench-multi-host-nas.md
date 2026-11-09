# Multi-host shared-filesystem contention

Run one single-threaded Python worker on each Linux SSH host against the same
externally mounted filesystem. The controller does not need the target mounted
locally. Mounting, mount options, credentials and unmounting are entirely external.
All hosts must see the **same absolute target path and shared namespace**.

## Run

The hosts file is private input read from `local/hosts.toml` (the fixed
`CONFIG["hosts"]` in `agent_fs_bench/benchmarks/multi_host.py`; `local/` is
git-ignored). Copy `experiments/hosts.example.toml` there and edit the host
entries. Select storage using the common targets file or `--dirs`:

```sh
uv run python -m agent_fs_bench run multi-host --dirs /mnt/shared
```

There are no workload tuning flags. Workload help accepts only `--help`:

```sh
uv run python -m agent_fs_bench run multi-host -- --help
```

The workload implementation is `agent_fs_bench/benchmarks/multi_host.py`. The
public interface is the common runner, not direct execution of that module.
Workload help opens no SSH connections. If `local/hosts.toml` is missing, the
run fails with a message pointing at `experiments/hosts.example.toml`.

### Hosts TOML

```toml
[[hosts]]
name = "node-a"
address = "node-a.example.net"
user = "bench"
port = 22
python = "python3"
ssh_args = ["-i", "/home/operator/.ssh/bench_ed25519", "-o", "ServerAliveInterval=30"]

[[hosts]]
name = "node-b"
address = "node-b.example.net"
user = "bench"
```

Only `[[hosts]]` entries are accepted; there is no mount plan or configuration
version. `name` and `address` are required. Names are unique safe path components
(letters, digits, `.`, `_`, `-`, starting with a letter or digit, at most 64
characters). Duplicate `(user, address, port)` endpoints are rejected.
`user` defaults to the SSH configuration, `port` to 22, `python` to `python3`, and
`ssh_args` to an empty array. SSH aliases in `address` are supported. `python`
is one executable name or absolute path, not a command with arguments.

`ssh_args` contains individual OpenSSH argv tokens, not a shell string; include
`-o` before each SSH option and `-i` before an identity path. Use an absolute
identity path; a literal `~` inside TOML is not shell-expanded. Authentication
must be noninteractive and SSH host keys must already be trusted. Result tables
record only logical host names, not SSH addresses, users or transport arguments.

### Code configuration

All counts, timeouts and toggles are constants in the `CONFIG` dictionary at the
top of `agent_fs_bench/benchmarks/multi_host.py`; edit them in source for a
deliberate experiment. Defaults are:

| `CONFIG` key | Default | Meaning |
| --- | --- | --- |
| `hosts` | `local/hosts.toml` | private TOML SSH host list |
| `files_per_host` | 1000 | private files per worker per round |
| `file_size_bytes` | 4096 | bytes in every file |
| `directories` | 32 | private subdirectories per round |
| `shared_files_per_host` | 200 | shared-directory files per worker per round |
| `rounds` | 3 | repetitions of the complete phase sequence |
| `fsync` | `True` | include file and directory fsync phases |
| `read_back` | `True` | include read phases |
| `cleanup_private` | `False` | delete successful worker private trees after timing |
| `worker_dir` | `/var/tmp/agent-fs-bench` | remote staging root, outside the measured target |
| `connect_timeout_s` | 15 | SSH connect timeout |
| `command_timeout_s` | 120 | preflight, deployment and setup timeout per command |
| `ready_timeout_s` | 180 | start-gate timeout |
| `run_timeout_s` | 1800 | worker alarm after GO; controller allows 60 extra seconds |
| `keep_remote` | `False` | retain uploaded package and internal job files |

## Execution and safety

1. Preflight every host over real SSH: require Linux, Python 3.12+, and a writable
   existing target directory. No sudo, mount utilities or remote checkout is needed.
2. Upload the actual Python package and a JSON **input job**, using SSH stdin,
   under `<worker-dir>/<run-id>/<target-name>/<host-name>/`. The worker imports
   shared package code; workers need no third-party Python dependency. Deployment
   uses a fresh directory with mode 0700.
3. Create the unique shared run directory on the first host:
   `<target>/agent-fs-bench/multi-host/<run-id>/`. Write a random owner sentinel;
   every host reads it and writes its own sentinel. Each worker must read all host
   sentinels before READY. This catches disconnected namespaces without depending
   on mount source strings. It does not verify a particular storage vendor, mount
   type or durability guarantee.
4. Start each worker with its configured interpreter, unbuffered:
   `python -u -m agent_fs_bench.benchmarks.multi_host --worker --job <job-path>`.
   The remote working directory is the uploaded package root. Every worker prints
   READY and waits for GO on SSH stdin. The controller releases all workers only
   after every READY. There are **no per-round or per-phase barriers**: independent
   workers free-run after the original start gate.
5. Collect the in-memory phase results over stdout, retain diagnostic logs locally,
   remove deployed package/job directories unless requested otherwise, and append
   rows to the `phases` and `latencies` tables. No raw result files are written to
   the shared filesystem.

The controller never creates the target path locally and never deletes the shared
run root or target root. Targets and staging paths must be absolute non-root paths
without `..`. Workers reject existing private directories and symlinked run
subdirectories. Run-root resolution must stay inside the target. These checks are
not protection against another process maliciously changing paths during a run.
The staging directory must not be on the measured target.

Shared filenames are uniquely owned by host/round/index; the timed delete phase
removes only that host's files. Private cleanup is opt-in and occurs after successful
work. Failed workers can leave partial files for diagnosis; remove only the selected
run directory manually after confirming no worker is still running. Repeated runs
with retained private files consume space. There are no retries or substitute
workloads. An operation failure, including unsupported directory fsync, fails the
run rather than producing successful rows with ignored errors. Set
`CONFIG["fsync"] = False` only when that is the intended measurement.

An interrupted controller closes SSH pipes and terminates local SSH processes.
That does not guarantee immediate remote termination: workers have alarms, but
blocked filesystem calls may delay them. Inspect workers/storage after interrupts.
Externally managed mounts are always left untouched.

## Workload and measurement boundaries

Each host performs the following sequence in each round:

| Phase | Operation |
| --- | --- |
| `private_create` | exclusively create private files with deterministic host-specific payload |
| `private_fsync` | fsync every private file, then the round root and subdirectories |
| `private_read` | read private files and verify their length |
| `private_stat` | stat each private file |
| `private_rename` | rename each private file in place |
| `shared_create` | exclusively create uniquely named files in one common directory |
| `shared_fsync` | fsync each owned shared file, then the common directory |
| `shared_read` | read owned shared files and verify their length |
| `shared_stat` | stat each owned shared file |
| `shared_delete` | unlink each owned shared file |

Private files are distributed across the configured subdirectories **plus the
round directory itself**. Shared workers operate on their own files, but contend
on a single directory. Each host is single-threaded; concurrency comes from hosts.

Directory setup, filename preparation and payload generation are outside create
phase timing. Create does not implicitly fsync. Per-operation and phase timing
use `perf_counter_ns`; phase timing includes the loop and sample collection, not
subsequent percentile calculation. Reads validate byte count, not content hashes.
No cache is dropped: reads may be served from client cache. Fsync semantics depend
on the client/filesystem/server; this is not a durability proof.

Percentiles use sorted samples at `round((n - 1) * fraction)`, not interpolation.
Python syscall-wrapper overhead is included. Compare the same harness/Python
version across configurations rather than interpreting this as raw storage limits.
Only per-phase and per-operation timers are retained. Worker/controller total
time, start-gate write spans and wall-clock timestamps are not collected as
result diagnostics. Clock synchronization is not required for the start gate.

## Results

The common runner writes `results/multi-host/<run-id>/manifest.json` and one CSV
per record kind. Host and phase identity are columns, not case strings:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | `host`, `phase`, `repeat` | `ops`, `elapsed_s`, `ops_per_s`, `bytes`, `mib_per_s`, `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `mean_ms` |
| `latencies` | `host`, `phase`, `repeat`, `sample` | `latency_ns` |

`repeat` is the 1-based round and `sample` indexes one operation within a
phase/round; `latencies` holds every per-operation sample. Percentiles in
`phases` use sorted samples at `round((n - 1) * fraction)`. The shared-sentinel
check gates READY and is not a result column. The manifest `config` holds the
workload sizes, rounds, fsync/read policy and hosts file path; host names are
already in the rows. No SSH settings, remote runtime/preflight dumps, paths,
timestamps or stdout/stderr are copied into tables. Local `<host>.stdout.log` and
`<host>.stderr.log` remain diagnostic attachments, not analysis inputs.

Both tables pool over `repeat`/`sample` into `summary/<table>.csv`, which holds
`n` and the median of each metric per target and remaining key columns (host,
phase). Host rows remain separate; environment consistency is the operator's
responsibility. A failed target's multi-host workload emits no
rows; there is no always-zero error-count metric.

Default figures in `figures/` split by phase and draw one box per host:

| Figure stem | Table | x | y |
| --- | --- | --- | --- |
| `phases-throughput-<phase>` | `phases` | host | throughput (Kops/s) from `ops_per_s` |
| `latencies-<phase>` | `latencies` | host | operation latency (ms) from `latency_ns`, log scale |

Re-render with `uv run python -m agent_fs_bench plot results/multi-host/<run-id>`.

Hosts free-run: summing their phase rates is not a synchronized server-wide
throughput measurement, and averaging host percentiles is not a pooled percentile.
Compare private versus shared phases, per-host tails, and scaling across 1/2/4/8
independent clients while keeping mount options and per-host work fixed. To hold
total work fixed instead, divide file counts by host count. Repeat configurations
to measure spread.

## Smoke strategies

A real local Linux SSH server with one host entry exercises upload, shared-package
imports, sentinel setup, the READY/GO protocol, workload, tables and remote cleanup.
The defaults are a full-sized run, so set the smoke values in `CONFIG` first:
`files_per_host = 8`, `file_size_bytes = 128`, `directories = 2`,
`shared_files_per_host = 4`, `rounds = 2`, `cleanup_private = True`. Then run:

```sh
uv run python -m agent_fs_bench run multi-host --dirs /mnt/smoke-share
```

Create `/mnt/smoke-share` externally first. With fsync/read enabled, expect 20
`phases` rows per host, 8 private operations (except private fsync: 11), and 4
shared operations (except shared fsync: 5), plus one `latencies` row per operation.
Two genuinely independent Linux hosts using an externally prepared common share
are required to test cross-client contention and namespace visibility. A mismatched
namespace should fail before any READY gate release and emit no measurement rows.

For a worker-only smoke, use a freshly prepared unique run directory and an input
job made with `_job_for_host` (the same input retained when `CONFIG["keep_remote"]`
is `True`). It must name the target/run root and matching owner/host sentinels;
an already completed job cannot be replayed over existing private files. From
the deployed package root:

```sh
printf 'GO\n' | python3 -u -m agent_fs_bench.benchmarks.multi_host --worker --job /absolute/path/job.json
```

The internal worker emits READY then one RESULT_JSON protocol message, not a public
result file. This isolated smoke does not test SSH, controller gates, deployment,
table collection or actual multi-client storage.
