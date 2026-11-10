# Run modes and task placement

Every workload runs its tasks as Open MPI jobs. `run --mode` selects the machines a
run may use:

| Mode | Machines, in fill order | Controller |
| --- | --- | --- |
| `local` (default) | the controller | runs tasks; `local/hosts.toml` is not read |
| `overflow` | the controller, then `local/hosts.toml` in file order | runs tasks first |
| `remote` | `local/hosts.toml` in file order | launches `mpiexec`, runs no task |

```sh
uv run python -m agent_fs_bench run mdtest --mode local --dirs /mnt/fs-a -- --smoke
uv run python -m agent_fs_bench run mdtest --mode overflow --dirs /mnt/fs-a
uv run python -m agent_fs_bench run mdtest --mode remote --dirs /mnt/fs-a
```

Each workload's `CONFIG['tasks']` is the external concurrency of one run: the number of
simultaneous independent tasks (mdtest ranks, clone requests, npm installs, workflow
instances, mount-scale clients). It is one value per run; draw a curve over N from
several runs pooled with `plot --vary tasks`. `mode` is part of `config.json`'s `config`,
so runs of different modes pool only with `--vary mode`.

## Machines

- Linux and Python 3.12+ on every task machine; the controller of `remote` must also be
  Linux with the same Open MPI.
- The same checkout at the same absolute path by default, each with its own
  `uv sync --locked` environment (it installs mpi4py). The controller compares every
  machine's source fingerprint with its own and stops on a difference, so sync the
  checkouts first. Each workload's `CONFIG` literal is excluded from the fingerprint;
  the controller's values are the ones used.
- Open MPI 4.1 on every machine, and mpi4py loading that same library. The controller
  checks both: on a machine where another MPI comes first (e.g. MPICH in
  `/usr/local/lib`), mpi4py silently loads it; set `CONFIG['env']` below.
- Distinct hostnames and one CPU architecture. Non-interactive SSH from the controller
  to each listed machine, and TCP between all participating machines (Open MPI's
  daemon and transport traffic, not only port 22).
- The workload's own tools on every machine that runs its tasks (see each workload
  doc). Nothing is installed automatically. The controller also needs Arial for `plot`.

## `local/hosts.toml`

Copy `experiments/hosts.example.toml`. List remote machines only, in fill order; never
list the controller.

| Field | Meaning |
| --- | --- |
| `name` | Logical machine name in results and `config.json`; `local` is reserved |
| `address` | Hostname or IPv4 address that Open MPI uses as the host token (not an SSH-only alias) |
| `user`, `port`, `ssh_args` | SSH account, port and extra argv tokens (e.g. `["-i", "/home/me/.ssh/key"]`) |
| `root` | Checkout path on that machine; default: the controller's checkout path |
| `python` | Interpreter; default: `<root>/.venv/bin/python` |

Open MPI starts each machine's daemon through the controller's inventory-aware SSH
agent (`agent_fs_bench/ssh.py`) with tree spawning disabled, so machines need no SSH
access to each other. No SSH credential or option is written to results, but the
per-job Open MPI appfile under `results/<bench>/<run-id>/<target>/` holds each
machine's Open MPI address, checkout path and Python path; remove or redact those
files before publishing results.

## Launcher settings

`CONFIG` at the top of `agent_fs_bench/cluster.py` describes the controller and its
network. `run` builds one `Launcher` from it and passes it explicitly to every probe and
job; nothing reads `CONFIG` afterwards:

| Key | Default | Meaning |
| --- | --- | --- |
| `hosts_file` | `local/hosts.toml` | Machine inventory |
| `mpiexec` | `('mpiexec',)` | Open MPI 4.1 launcher, e.g. `('/usr/bin/mpiexec.openmpi',)` |
| `mpiexec_args` | `()` | Extra launcher options, e.g. interface selection |
| `env` | `{}` | Environment of every probe, task and native program (`-x` for MPI jobs) |
| `connect_timeout_s` | `15` | SSH connect timeout |

`mpiexec`, `mpiexec_args` and `env` change measurements, so `config.json` records their
values as `config.launcher`; `plot` pools runs with different launcher settings only
with `--vary launcher`. Interface subnets in `mpiexec_args` therefore appear in results.
`config.json` also records the launcher's Open MPI version (`mpi_version`) and every
machine's probed facts (`nodes`: name, hostname, CPU model, CPU count, system, kernel,
architecture, Python, mpi4py and Open MPI library).

On machines with several interfaces, or with identical bridge addresses (`docker0`,
`virbr0`), Open MPI may dial the wrong address and hang. Restrict it to the shared
subnet, and pin mpi4py's library where another MPI is installed:

```python
CONFIG = {
    'hosts_file': PROJECT_ROOT / 'local' / 'hosts.toml',
    'mpiexec': ('/usr/bin/mpiexec.openmpi',),
    'mpiexec_args': ('--mca', 'btl_tcp_if_include', '10.0.0.0/24',
                     '--mca', 'oob_tcp_if_include', '10.0.0.0/24'),
    'env': {'MPI4PY_MPIABI': 'openmpi'},
    'connect_timeout_s': 15,
}
```

## Placement

A workload declares `tasks`, a positive integer:

- Tasks are dealt round-robin over the mode's machines: machine i of M gets
  `tasks // M`, plus one if `i < tasks % M`. Every machine gets the same share
  whatever its CPU count; with fewer tasks than machines only the first ones are used.
  MPI ranks are contiguous per machine.
- Tasks are not bound to CPUs. The kernel schedules each machine's tasks over its CPUs
  (its `sched_getaffinity` set, so `taskset` and cpusets apply). Tasks are filesystem
  clients that block on I/O; letting a runnable task move to an idle CPU is how N real
  concurrent clients behave. There is no capacity limit: tasks above the CPU count
  oversubscribe the machine.
- A task's internal parallelism (dev-workflow's `make -j`, npm-install's thread pool)
  is the workload's own setting (`jobs`, `concurrency`), not a placement setting.
- Waiting ranks yield (`mpi_yield_when_idle`, always set): the appfile declares exactly
  each machine's tasks, so Open MPI does not detect oversubscription itself. Many tasks
  per machine need high enough `ulimit -u` / `ulimit -n`; Python workloads cost one
  interpreter per task.
- No CPU is reserved for the controller, Python, MPI or filesystem daemons. Formal runs
  should fix cpuset, SMT, NUMA, machine model and background load.

For example, with 8 CPUs on the controller and 64 on `node-a` in `overflow` mode:

- `tasks = 1`: one task on the controller; node-a is unused.
- `tasks = 144`: 72 tasks on each machine, about 9 per CPU on the controller and about
  1.1 on node-a.
- `remote` with only node-a and `tasks = 1024`: 1024 tasks over 64 CPUs.

## Launch and validity

One launch is one global Open MPI MPMD job with one appfile line per task
(`-np 1 --host <machine>`), so the rank is the line number and its machine is fixed by
the plan. The run stops at the first failed assertion, with label, expected and measured
value:

| Check | When | Expected |
| --- | --- | --- |
| pool | after probing | ≥ 1 machine, distinct hostnames, one architecture, Linux, the controller's source fingerprint, mpi4py loading the launcher's Open MPI 4.1 |
| tasks | placement | positive integer |
| placement | after a Python job | every rank's hostname equals its planned machine |
| job | after every job | exit status 0; Python jobs also the completion message |
| binary | before native jobs | one SHA-256 of the native binary on every participating machine |

`targets[].info.placement` records the task count per machine.

- **Python workloads** (clone, npm-install, kvtest, sqlite-speedtest, dev-workflow,
  mini-dev-workflow, agent-replay) run one MPI job per target, and every task runs
  `python -m agent_fs_bench.cluster task`, which calls the workload's `task(t)`.
  - Task 0 receives the `JobSpec` on stdin and streams result messages to the
    controller on stdout; the protocol table is in the `cluster.py` docstring.
  - mpi4py barriers start a batch on all tasks together and end it after the slowest,
    which gives the batch wall time.
  - Clocks of different machines are never compared; per-task durations are measured
    by each task on its own machine.
- **Native MPI programs** are started by `mpiexec` directly: mdtest and mdtest-peak one
  MPI job per repeat; mount-scale one job per shared fixture, then per repeat one warm-up
  job per shared resource, the private job and one job per shared resource.
  - Before a job the controller creates the directories mdtest expects on each machine
    (`mkdir -p` over SSH); after it, the controller fetches rank 0's CSV from rank 0's
    machine.
  - mount-scale also keeps one lifecycle worker per participating machine (over SSH
    for remote machines) for mounts, sampling and cleanup between the jobs.

## Directories and data

- **Controller:** `results/<bench>/<run-id>/` holds `config.json` and the tables. Launcher
  logs (appfiles, stdout and stderr) go in `<target>/`; `plot` writes `figures/` there.
- **Each task machine:** work goes to `<target>/agent-fs-bench/<bench>/<run-id>/<node>/`,
  with one `task-NNNN/` directory per Python task.
  - Diagnostics stay in that machine's checkout under
    `results/<bench>/<run-id>/<target>/hosts/<node>/`.
  - They are not copied back; mount-scale collects its lifecycle logs.
- **Data preparation:** each machine prepares its own data (sources, seeds, builds) in
  its own directories and never reads data prepared on another machine.
  - The experimenter guarantees what the target path is on each machine: one shared
    filesystem, or separate local disks.
  - Experiments that need one directory seen by every task need a shared target:
    mdtest's `shared` layout and mount-scale's `admin_root`.

Any error stops the run. A failing task aborts the whole MPI job, and the controller
reports the tail of the launcher stderr. There is no retry, failover or smaller N; work
directories and diagnostics stay for inspection.
