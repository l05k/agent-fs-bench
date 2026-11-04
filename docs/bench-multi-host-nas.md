# Multi-host NAS contention experiment

Measures what several independent Linux hosts see when they mount the same NAS
and write to it at the same time.

测量多台相互独立的 Linux 服务器同时挂载同一个 NAS、并发读写时，每台主机看到的
吞吐与尾延迟，以及所有主机在同一共享目录里竞争元数据操作时的表现。

Unlike the single-host benchmarks in this repository, this is an orchestrated
experiment. The machine that runs `multi_host_nas_fs.py` is the control plane;
it never needs a local NAS mount. It deploys the same self-contained script as a
worker to every host listed in YAML, mounts the NAS there, releases all workers
together, and collects their reports.

与仓库中的单机 benchmark 不同，这是一个编排型实验。运行
`multi_host_nas_fs.py` 的机器只作为控制机，本地不需要 NAS 挂载；脚本会把自身
作为 worker 部署到 YAML 中的每台服务器，在这些服务器上挂载 NAS、统一放行，
最后回收各主机的结果。

## Artifacts / 产物

| Path | Where | Purpose |
| --- | --- | --- |
| `multi_host_nas_fs.py` | repo root | control plane, YAML parser and remote worker |
| `experiments/multi-host-nas.yaml` | repo | example SSH/NAS/workload plan |
| `<nas.run_root>/<run-id>/hosts/<host>/` | NAS | private trees written by each host |
| `<nas.run_root>/<run-id>/shared/` | NAS | one directory all hosts contend on |
| `<nas.run_root>/<run-id>/_results/<host>.json` | NAS | per-host archival result |
| `results/multi_host_nas_fs/<UTC-run-id>/` | control machine | reports and logs |

The result directory contains `result.json`, `metrics.csv`, `aggregate.csv`,
`summary.txt`, `manifest.txt`, `config.yaml`, `plan.json`, one
`<host>.result.json` per host, and `logs/<host>.{stdout,stderr}.log`.

## Plan / 计划文件

```yaml
version: 1

experiment:
  name: shared-nas-contention

ssh:
  user: bench
  identity_file: ~/.ssh/id_ed25519
  options:
    - StrictHostKeyChecking=accept-new
    - ServerAliveInterval=30

hosts:
  - name: node-a
    address: 10.10.0.11
  - name: node-b
    address: 10.10.0.12

nas:
  source: nas01.example.com:/export/agent-fs
  fstype: nfs
  options: vers=4.2,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2
  mountpoint: /mnt/agent-fs-nas
  run_root: /mnt/agent-fs-nas/agent-fs-bench/shared-nas
  privileged: true
  verify_source: true

workload:
  files_per_host: 512
  file_size_bytes: 8192
  directories: 16
  shared_files_per_host: 256
  rounds: 3
  fsync: true
  read_back: true
  cleanup_private: false

remote:
  python: python3
  worker_dir: /var/tmp/agent-fs-bench
  ready_timeout_s: 180
  run_timeout_s: 1800
```

Required keys are `experiment.name`, `hosts`, `nas.source`, `nas.mountpoint`,
`nas.run_root`, and the `workload` counts. `nas.run_root` must be inside
`nas.mountpoint`; the script refuses a plan whose test path could escape onto a
local filesystem. Relative remote paths are also rejected.

必填项是 `experiment.name`、`hosts`、`nas.source`、`nas.mountpoint`、
`nas.run_root` 以及各 workload 数量。`nas.run_root` 必须位于
`nas.mountpoint` 内，否则脚本会拒绝，以防测试数据误写到本地盘。

Common optional fields and defaults:

| Field | Default | Meaning |
| --- | --- | --- |
| `ssh.user` | none | default SSH user; a host can override it |
| `ssh.identity_file` | none | private key passed to `ssh -i` |
| `ssh.options` | none | extra `ssh -o KEY=VALUE` options |
| `ssh.connect_timeout_s` | `15` | SSH connect timeout |
| `ssh.command_timeout_s` | `120` | timeout for deploy and setup commands |
| `hosts[].port` | `22` | per-host SSH port |
| `hosts[].options` | none | per-host extra SSH options |
| `nas.fstype` | auto | passed as `mount -t` when set |
| `nas.options` | none | passed as `mount -o` when set |
| `nas.privileged` | `true` | prefix mount operations with `sudo -n --` |
| `nas.verify_source` | `true` | require an existing mount's source to match |
| `nas.mount_timeout_s` | `120` | parallel mount timeout per host |
| `nas.unmount_timeout_s` | `60` | parallel unmount timeout per host |
| `nas.lazy_unmount` | `false` | add `umount -l` |
| `nas.unmount_on_exit` | `true` | unmount mounts owned by this run |
| `workload.rounds` | required | repeat the full phase set |
| `workload.fsync` | `true` | run `*_fsync` phases |
| `workload.read_back` | `true` | run `*_read` phases |
| `workload.cleanup_private` | `false` | remove each host's private tree |
| `remote.python` | `python3` | remote interpreter command/absolute path |
| `remote.worker_dir` | `/var/tmp/agent-fs-bench` | remote staging root |
| `remote.ready_timeout_s` | `180` | maximum wait before all workers are ready |
| `remote.run_timeout_s` | `1800` | remote workload alarm after the gate |

The built-in YAML reader deliberately supports a strict subset: block
mappings/sequences, comments, plain or quoted scalars, booleans, null, integers
and floats. Anchors, aliases, flow collections (`[...]`, `{...}`), folded or
literal multiline scalars, and multi-document streams are rejected with a line
number. This keeps the control script standard-library-only and the accepted
configuration unambiguous.

## Prerequisites / 前置条件

- The control machine has OpenSSH `ssh` and can log in to every host without an
  interactive password (key-based or agent authentication).
- Every target is Linux and has Python 3.9 or newer.
- Every target has `mountpoint`, `findmnt`, `df`, and the userspace mount helper
  for the NAS protocol (for example `nfs-common` for NFS or `cifs-utils` for
  SMB).
- If `nas.privileged: true`, passwordless `sudo -n true` works on every host.
- The NAS is reachable from every host and `nas.source` is the source string
  that `mount` accepts there.

The plan is validated locally first:

```sh
python3 multi_host_nas_fs.py \
  --config experiments/multi-host-nas.yaml --check
```

`--check` parses and prints the normalized topology, but opens no SSH
connections, mounts nothing and writes nothing to the NAS.

Only after that should the experiment be launched from a control machine that
has the real SSH/NAS environment:

```sh
python3 multi_host_nas_fs.py \
  --config experiments/multi-host-nas.yaml
```

Use `--keep-mounted` to leave mounts created by the controller in place, or
`--keep-remote` to retain the uploaded worker and job files.

## Execution / 执行流程

1. **Preflight (parallel).** Check SSH, Python, required commands, the mount
   helpers, and `sudo -n` where configured.
2. **Deploy (parallel).** Write this script and a JSON job file below
   `remote.worker_dir/<run-id>/<host>/`; the existing remote repository is not
   needed.
3. **Mount (parallel).** Create the mountpoint if absent, then run the
   configured `mount`. An existing mount is accepted only when its source
   matches `nas.source` (unless `verify_source: false`); it is never unmounted
   by this run. New mounts are checked against the same source rule. A source
   mismatch aborts the experiment before any workload.
4. **Start gate.** Start one worker over SSH on every host. Each worker prints
   `READY` and blocks on stdin. After every host is ready, the controller writes
   one `GO` line to all SSH stdin pipes, so all hosts are released together.
   `start_gate_control_write_ms` records the controller-side span of those
   writes. There is no per-phase barrier after the gate.
5. **Workload.** Each host runs the same phases against
   `<run_root>/<run-id>/hosts/<host>/` and the common `shared/` directory.
6. **Collect and clean up.** The controller parses `RESULT_JSON` from stdout,
   saves per-host logs, unmounts only mounts it created, and removes its remote
   staging directory. Private trees stay on the NAS unless
   `workload.cleanup_private: true`; shared test files are always removed by
   their owner.

If preflight or any mount fails, the workload is not started. Successfully
mounted hosts are unmounted during cleanup. Interrupting the controller closes
the start gate and terminates the local SSH processes.

## Workload / 工作负载

All hosts start at the same gate but then free-run; this models independent
clients rather than a globally lock-stepped benchmark.

| Phase | Location | Operation |
| --- | --- | --- |
| `private_create` | per-host tree | create `files_per_host` files across `directories` |
| `private_fsync` | per-host tree | `fsync` every file, then the directories |
| `private_read` | per-host tree | read every file back and check its length |
| `private_stat` | per-host tree | `stat` every file |
| `private_rename` | per-host tree | rename every file in place |
| `shared_create` | common `shared/` | create unique files beside every other host |
| `shared_fsync` | common `shared/` | `fsync` every shared file and the directory |
| `shared_read` | common `shared/` | read every shared file back |
| `shared_stat` | common `shared/` | `stat` every shared file |
| `shared_delete` | common `shared/` | unlink every shared file |

`fsync` and `read_back` can disable their corresponding optional phases. Each
host is single-threaded; concurrency comes from the number of hosts. Every file
is the same size (`file_size_bytes`), and each worker's payload is deterministic
from its host name.

The private phase measures normal per-client behavior. The shared phase is the
contention probe: all hosts create and manipulate uniquely named files in one
directory, so directory creation, lookup, cache invalidation and server-side
metadata locking are exercised across clients.

## Suggested experiment matrix / 建议实验矩阵

Run the same plan with one host, then two, four, and eight hosts while holding
the NAS mount options fixed. Keep `files_per_host` constant to measure how
aggregate client throughput and tail latency scale with added clients; divide
it by the host count to keep total bytes/files constant and isolate contention.
Repeat each configuration enough times to report a median and spread.

固定 NAS 挂载参数后，分别用 1、2、4、8 台主机运行同一计划：保持
`files_per_host` 不变可观察总吞吐与尾延迟随客户端数的扩展；按主机数同比例
缩小 `files_per_host` 则可在总写入量不变的条件下隔离竞争效应。每种配置重复
多次，报告中位数与离散度。

The interesting signals are the difference between `private_*` and `shared_*`
phases, the growth of `max_host_p95_ms`, the gap between
`sum_host_ops_per_s` and linear scaling, and any `errors` reported by fsync
phases.

## Output / 输出

`metrics.csv` has one row per host/round/phase:

`host, round, phase, ops, errors, elapsed_s, ops_per_s, bytes, mib_per_s,
p50_ms, p95_ms, p99_ms, max_ms, mean_ms`

`errors` counts failed `fsync` calls; they are reported rather than aborting
the whole run, because some NAS clients reject directory fsync. Other operation
errors fail the worker.

`aggregate.csv` groups those rows by round and phase:

`round, phase, hosts, total_ops, total_bytes, total_errors, sum_host_ops_per_s,
max_host_elapsed_s, max_host_p95_ms`

Because phases free-run after the start gate, `sum_host_ops_per_s` is the sum of
each host's measured phase rate, not a server-wide rate over a perfectly aligned
window. Use `max_host_p95_ms` as a conservative tail-latency indicator. The JSON
results retain every raw phase row and the mount/preflight details.

`result.json` records each host's `gate_wait_s`; the duplicated full worker
payload, including every raw phase row, lives in `<host>.result.json`.

## Safety and limitations / 安全与限制

- Workers remove only the `shared/` files they created, after the timed delete
  phase completes; a failed worker can leave its partial files behind. The
  controller never deletes the NAS run root. `cleanup_private` is opt-in, so
  repeated experiments can fill the NAS.
- `nas.options` may contain credentials if configured badly. Prefer a
  credentials file for CIFS and do not put passwords in YAML; the plan is
  copied into local results verbatim.
- Existing mounts are treated as externally owned. Their source is checked and
  they are not unmounted automatically.
- Read phases use the page cache and are not cold-cache measurements. A NAS
  client can return writes/reads from its local cache before the server has
  committed them.
- Per-operation latency includes the Python harness's syscall wrapper overhead.
  Compare configurations measured with the same script and Python version, not
  raw NAS vendor numbers.
- The start gate synchronizes release, not the start of every workload phase.
  Clock skew does not affect the gate because it is an SSH stdin signal.
- If a controller or SSH connection dies, a running remote worker may continue
  until its `run_timeout_s` alarm. Check the target hosts and the NAS run root
  after an interrupted run.
- This experiment is not a replacement for vendor NAS benchmarks or a
  durability test. It is a reproducible concurrency probe for the same workload
  shape across a set of machines.
