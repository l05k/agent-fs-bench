# agent-fs-bench

Filesystem benchmarks for agent workloads. Each workload runs against an
already-mounted directory, so local disks, FUSE and NFS share one interface
(`mount-scale` can also run real mount commands). Every workload dispatches its
concurrent tasks as Open MPI jobs over this controller and optional SSH machines
(`run --mode`). Each run writes a parameter snapshot (`config.json`) and flat raw CSV
tables; the separate `plot` command draws paper-style figures from them.

Formal runs target Linux with Python 3.12+ and Open MPI 4.1 on every machine. The
repository contains no internal filesystem implementation or credentials. Every
validity check is an `assert` (so `python -O` is refused); any failure stops the run.

## Install

```sh
uv sync --locked
uv run python -m agent_fs_bench --help
```

`uv sync --locked` installs mpi4py, which loads the machine's Open MPI 4.1 at run
time. Only the machine running `plot` needs Matplotlib and Arial (Ubuntu:
`sudo apt install ttf-mscorefonts-installer`); `run` does not render figures.

Deploy the whole checkout, including `data/`, on every machine that runs tasks.
External tools are prepared by the experimenter; each workload doc lists its
requirements. `agent-replay` needs a submodule; see its doc.

## Targets

Mount filesystems yourself, then copy `experiments/targets.example.toml` to
`local/targets.toml`:

```toml
[[targets]]
name = "fs-a"        # label in results and figures
path = "/mnt/fs-a"   # shorthand: path = "fs-a"
```

The benchmark does not verify that a path is actually mounted.

```sh
# All targets in local/targets.toml, in order
uv run python -m agent_fs_bench run clone -- --smoke
# One registered alias (repeatable)
uv run python -m agent_fs_bench run clone --target fs-a -- --smoke
# Explicit directories; alias = last path component
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a /mnt/fs-b -- --smoke
```

Targets run sequentially. Each machine works in
`<target>/agent-fs-bench/<bench>/<run-id>/<node>/`; results go to
`results/<bench>/<run-id>/` on the controller.

## Run modes

```sh
uv run python -m agent_fs_bench run mdtest --mode local --dirs /mnt/fs-a     # this machine (default)
uv run python -m agent_fs_bench run mdtest --mode overflow --dirs /mnt/fs-a  # this machine, then local/hosts.toml
uv run python -m agent_fs_bench run mdtest --mode remote --dirs /mnt/fs-a    # local/hosts.toml only
```

Each workload's `CONFIG['tasks']` is the external concurrency of one run (N
simultaneous tasks); draw curves over N from several runs with `plot --vary tasks`.
Tasks are dealt round-robin over the machines and are not bound to CPUs; the kernel
schedules them, and N above the CPU count oversubscribes. List remote machines in
`local/hosts.toml` (see `experiments/hosts.example.toml`); every machine needs the same
checkout. Setup, placement and launcher settings: [run modes](docs/run-modes.md).

The controller records the launcher, node facts and planned targets before
running any workload. It appends validated raw observations and marks a target
complete only after the workload returns. Mount-scale runs its two synchronized
I/O batches under the mount lifecycle; mdtest has its own phase counters. The
per-workload docs define each timing boundary.

## Workloads

| Command | Workload | Doc |
| --- | --- | --- |
| `dev-workflow` | N concurrent Linux kernel workflows: build, search, edit, Git, tree check and final filesystem sync | [dev workflow](docs/bench-dev-workflow.md) |
| `mini-dev-workflow` | Same Git worktree workflow on a small C project | [dev workflow](docs/bench-dev-workflow.md) |
| `npm-install` | N concurrent offline npm or pnpm installs into cold `node_modules` | [npm install](docs/bench-npm-install.md) |
| `npm-install-nofsync` | Same, with no harness fsync; install timing, throughput and tree stats, no flush/durable metrics | [npm install](docs/bench-npm-install.md#no-fsync-variant) |
| `clone` | Durable directory copies and native LakeBase Clone OpenAPI latency | [clone](docs/bench-clone.md) |
| `kvtest` | SQLite small-BLOB reads and checked synchronous overwrites | [kvtest](docs/bench-kvtest-openclaw.md) |
| `sqlite-speedtest` | N independent SQLite `speedtest1 main` SQL phases | [sqlite-speedtest](docs/bench-sqlite-speedtest.md) |
| `mount-scale` | N independent mounts running remounted whole-file reads and batch-durable creates | [mount scale](docs/bench-mount-scale.md) |
| `mdtest` | POSIX file or directory metadata throughput of N tasks at fixed total work | [mdtest](docs/bench-mdtest.md) |
| `mdtest-peak` | Weak-scaling file create/stat/read/unlink ops/s and MiB/s of N tasks, up to 2× CPUs | [mdtest peak](docs/bench-mdtest-peak.md) |
| `agent-replay` | Paired Codex/OpenClaw model-response replay with native tool timing and per-trial verification | [agent replay](docs/bench-agent-replay.md) |

```sh
uv run python -m agent_fs_bench run <command> --dirs /mnt/fs-a
```

Workload flags (after `--`): `--smoke` for `clone`, `mdtest`, `mdtest-peak`,
`kvtest` and `sqlite-speedtest`, and `--directory-smoke` for `mount-scale`. All other
settings (tasks, scale, repeats, PRAGMAs, tool versions, paths) live in the `CONFIG`
dict at the top of each module in `agent_fs_bench/benchmarks/`. Changing an
experiment means editing `CONFIG`; each run's `config.json` records the resulting
`config` (plus `mode`) and a source fingerprint. Keep private values out of commits.

`mount-scale` also needs `local/mounts.toml`.

## Results

```text
results/<bench>/<run-id>/
  config.json          # seed, git commit, config, machine facts, per-target info
  <table>.csv          # one row per observation: key columns, then metrics with units
  <target>/            # launcher logs and diagnostics
```

Each table has one granularity (e.g. `phases` per repeat × operation, `ranks` per
rank); every metric cell is a finite number. `run` never aggregates. Draw figures
(median and IQR of the raw rows), pool runs with identical configs and sources, or
draw a custom figure:

```sh
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --targets ours,ext4,nfs
uv run python -m agent_fs_bench plot results/mdtest/<run-n1> results/mdtest/<run-n16> \
  --vary tasks --table phases --x tasks --y ops_per_s --split operation,layout \
  --yscale 1000 --logx
```

Columns and plot options: [results and figures](docs/results-and-analysis.md).

For a test machine reached only by copy/pasting one Markdown document, start with
the self-contained [read-only inventory request](experiments/00-inventory-request.md).
It preserves old code, local experiment changes, failure experience and historical
data before any upgrade. Then use the [single-document exchange workflow](docs/markdown-exchange.md):
a manual plan in, reviewed raw data out, local analysis and plotting afterward.
`python3 -m agent_fs_bench.exchange` packages/imports reviewed UTF-8 text using only
the standard library; it never runs the copied commands or renders figures.

## Reproducibility

- Archive the checkout, `uv.lock`, `local/` configs (with the launcher `CONFIG` of
  `agent_fs_bench/cluster.py`) and result directories. The source fingerprint covers
  uncommitted changes, except each module's `CONFIG` literal. A workload's `CONFIG`
  is recorded as `config`; the launcher `CONFIG` as `config.launcher`, the values of
  `mpiexec`, `mpiexec_args` and `env`. Neither replaces a source archive.
- `plot` pools runs only when their source fingerprints match and their `config`
  matches apart from keys given to `--vary`; `mode` and `launcher` are part of
  `config`. It does not
  compare tool versions (in each target's `info`), kernel, mount options or host
  environment; keep those consistent yourself. `config.json` records each machine's
  probed facts (`nodes`, incl. CPU model) and every target's task count per machine.
- Cache semantics are workload-specific. `mount-scale` remounts the clients
  before reads; this does not evict server or persistent/shared client caches.
- Tasks are not bound to CPUs; resource limits (cgroups, cpusets) for FUSE clients,
  servers and the benchmark are set externally.
- Smoke runs only prove the pipeline works; they are not performance or
  consistency results.

```sh
uv run python -m unittest discover -s tests
```

## Layout

```text
agent_fs_bench/   CLI, task placement and Open MPI dispatch (cluster.py), analysis,
                  plotting; benchmarks/ holds one module per workload
experiments/      example TOML configs (copy to local/, which is git-ignored)
data/             fixed inputs, lockfiles, Git bundles (see docs/fixtures.md)
third_party/      agent-mitm-replay submodule
tools/            fixture maintenance
docs/             run modes, per-workload configuration and timing boundaries
```
