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
several runs pooled with `plot --vary tasks`. `mode` is part of the manifest `config`,
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
  doc). Nothing is installed automatically. The controller also needs Arial for figures.

## `local/hosts.toml`

Copy `experiments/hosts.example.toml`. List remote machines only, in fill order; never
list the controller.

| Field | Meaning |
| --- | --- |
| `name` | Logical machine name in results and manifests; `local` is reserved |
| `address` | Hostname or IPv4 address that Open MPI uses as the host token (not an SSH-only alias) |
| `user`, `port`, `ssh_args` | SSH account, port and extra argv tokens (e.g. `["-i", "/home/me/.ssh/key"]`) |
| `root` | Checkout path on that machine; default: the controller's checkout path |
| `python` | Interpreter; default: `<root>/.venv/bin/python` |

Open MPI starts each machine's daemon through the controller's inventory-aware SSH
agent (`agent_fs_bench/ssh.py`) with tree spawning disabled, so machines need no SSH
access to each other. The inventory is never written to results.

## Launcher settings

`CONFIG` at the top of `agent_fs_bench/cluster.py` describes the controller and its
network; its values are not written to the manifest:

| Key | Default | Meaning |
| --- | --- | --- |
| `hosts` | `local/hosts.toml` | Machine inventory |
| `mpiexec` | `('mpiexec',)` | Open MPI 4.1 launcher, e.g. `('/usr/bin/mpiexec.openmpi',)` |
| `mpiexec_args` | `()` | Extra launcher options, e.g. interface selection |
| `env` | `{}` | Environment set in every probe and task before MPI loads |
| `connect_timeout_s`, `probe_timeout_s` | `15`, `120` | SSH connect and machine probe timeouts |

The manifest records the launcher name and version (`launcher`) and every machine's
probed facts (`nodes`: name, hostname, CPU affinity list, kernel, architecture, Python,
mpi4py and Open MPI library). `mpiexec`, `mpiexec_args` and `env` change measurements,
so `config.launcher_sha256` holds their SHA-256 without the values: `plot` pools runs
with different launcher settings only with `--vary launcher_sha256`.

On machines with several interfaces, or with identical bridge addresses (`docker0`,
`virbr0`), Open MPI may dial the wrong address and hang. Restrict it to the shared
subnet, and pin mpi4py's library where another MPI is installed:

```python
CONFIG = {
    'hosts': PROJECT_ROOT / 'local' / 'hosts.toml',
    'mpiexec': ('/usr/bin/mpiexec.openmpi',),
    'mpiexec_args': ('--mca', 'btl_tcp_if_include', '10.0.0.0/24',
                     '--mca', 'oob_tcp_if_include', '10.0.0.0/24'),
    'env': {'MPI4PY_MPIABI': 'openmpi'},
    'connect_timeout_s': 15,
    'probe_timeout_s': 120,
}
```

## Placement

A workload declares `tasks`, `cpus_per_task` (default 1; multi-threaded tasks declare
their width, e.g. npm-install 4, dev-workflow 8) and `oversubscribe` (default 1;
mdtest-peak 2):

- A machine's capacity is `floor(available CPUs / cpus_per_task) × oversubscribe`.
  Available CPUs are that machine's `sched_getaffinity` set, so `taskset` and cpusets
  apply.
- Machines fill in mode order. Within a machine, the CPUs are sorted and cut into groups
  of `cpus_per_task`, and every group gets one task before any group gets a second.
- `tasks = None` fills the whole capacity. A request above it is an error; a run never
  shrinks N or silently shares CPUs.
- No CPU is reserved for the controller, Python, MPI or filesystem daemons. Formal runs
  should fix cpuset, SMT, NUMA, machine model and background load.

For example, with 8 CPUs on the controller and 8 on `node-a` in `overflow` mode:

- `tasks = 12`: 8 tasks on the controller's CPUs 0–7 and 4 on node-a's CPUs 0–3.
- `tasks = 3` with `cpus_per_task = 4`: the controller runs two tasks on CPUs {0–3}
  and {4–7}, node-a one on {0–3}. The capacity is 4, so `tasks = 12` is an error.
- `tasks = 16` with `oversubscribe = 2`: all 16 run on the controller, two per CPU,
  and node-a is unused; `tasks = None` places 16 on each machine.

## Launch and placement proof

One launch is one global Open MPI MPMD job with one application context per task, so
`--host` pins each task to its machine. Before the task runs, a wrapper:

1. checks the actual MPI rank and hostname;
2. requires the planned CPUs to be inside the machine's affinity set;
3. pins them and reads the affinity back;
4. reports that measured placement.

The controller compares every report with the plan and stores it per job under
`targets[].info.placement`; a mismatch fails the run.

- **Python workloads** (clone, npm-install, kvtest, sqlite-speedtest, dev-workflow,
  mini-dev-workflow, agent-replay) run one MPI job per target, and every task calls the
  workload's `task(t)`.
  - Task 0 receives the job on stdin and streams result rows to the controller on
    stdout.
  - mpi4py barriers start a batch on all tasks together and end it after the slowest,
    which gives the batch wall time.
  - Clocks of different machines are never compared; per-task durations are measured
    by each task on its own machine.
- **Native MPI programs** run their own jobs: mdtest and mdtest-peak one MPI job per
  repeat; mount-scale one job per shared fixture, then per repeat one warm-up job per
  shared resource, the private job and one job per shared resource.
  - The wrapper reports the binary's SHA-256 and then execs it; every task must run
    the same binary.
  - mdtest's rank CSV is written on task 0's machine, and the wrapper returns its text
    through the launcher output.
  - mount-scale also keeps one lifecycle worker per participating machine (over SSH
    for remote machines) for mounts, sampling and cleanup between the jobs.

## Directories and data

- **Controller:** `results/<bench>/<run-id>/` holds the manifest, tables, summaries and
  figures. Launcher logs (appfiles, stdout and stderr) go in `<target>/`.
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
