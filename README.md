# agent-fs-bench

Filesystem benchmarks for agent workloads. Each workload runs against an
already-mounted directory, so local disks, FUSE and NFS share one interface
(`mount-scale` can also run real mount commands). Every workload dispatches its
concurrent tasks as Open MPI jobs over this controller and optional SSH machines
(`run --mode`). Each run writes a manifest and flat CSV tables, then per-group
medians and paper-style figures.

Formal runs target Linux with Python 3.12+ and Open MPI 4.1 on every machine. The
repository contains no internal filesystem implementation or credentials. Any error
stops the run; fix the environment and rerun.

## Install

```sh
uv sync --locked
uv run python -m agent_fs_bench --help
```

`uv sync --locked` installs mpi4py, which loads the machine's Open MPI 4.1 at run
time. Figures need the Arial font (Ubuntu: `sudo apt install ttf-mscorefonts-installer`).
Without it, `run` fails after the CSV tables are written; re-plot them elsewhere.

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
Tasks are pinned to logical CPU groups (`cpus_per_task`), machines fill in order and
a request above capacity is an error. List remote machines in `local/hosts.toml`
(see `experiments/hosts.example.toml`); every machine needs the same checkout.
Setup, placement and launcher settings: [run modes](docs/run-modes.md).

## Workloads

| Command | Workload | Doc |
| --- | --- | --- |
| `dev-workflow` | N concurrent Linux kernel workflows: configure, build, search, edit, commit, tree check | [dev workflow](docs/bench-dev-workflow.md) |
| `mini-dev-workflow` | Same Git worktree workflow on a small C project | [dev workflow](docs/bench-dev-workflow.md) |
| `npm-install` | N concurrent offline npm or pnpm installs into cold `node_modules` | [npm install](docs/bench-npm-install.md) |
| `npm-install-nofsync` | Same, with no harness fsync; install timing, throughput and tree stats, no flush/durable metrics | [npm install](docs/bench-npm-install.md#no-fsync-variant) |
| `clone` | N concurrent durable same-filesystem clones of one directory shape | [clone](docs/bench-clone.md) |
| `kvtest` | N independent SQLite `kvtest` random BLOB overwrites, per-write or batched commits | [kvtest](docs/bench-kvtest-openclaw.md) |
| `sqlite-speedtest` | N independent SQLite `speedtest1 main` SQL phases | [sqlite-speedtest](docs/bench-sqlite-speedtest.md) |
| `mount-scale` | N mdtest clients per mount topology; Open MPI mdtest and mount lifecycle | [mount scale](docs/bench-mount-scale.md) |
| `mdtest` | POSIX file or directory metadata throughput of N tasks at fixed total work | [mdtest](docs/bench-mdtest.md) |
| `mdtest-peak` | Weak-scaling file create/stat/read/unlink ops/s and MiB/s of N tasks, up to 2× CPUs | [mdtest peak](docs/bench-mdtest-peak.md) |
| `agent-replay` | N concurrent real Harbor/Docker tasks driven by accepted model-response cassettes | [agent replay](docs/bench-agent-replay.md) |

```sh
uv run python -m agent_fs_bench run <command> --dirs /mnt/fs-a
```

Workload flags (after `--`): `--smoke` for `clone`, `mdtest`, `mdtest-peak`,
`kvtest` and `sqlite-speedtest`, and `--directory-smoke` for `mount-scale`. All other
settings (tasks, scale, repeats, PRAGMAs, tool versions, paths) live in the `CONFIG`
dict at the top of each module in `agent_fs_bench/benchmarks/`. Changing an
experiment means editing `CONFIG`; each run's manifest records the resulting
`config` (plus `mode`) and a source fingerprint. Keep private values out of commits.

`mount-scale` also needs `local/mounts.toml`.

## Results

```text
results/<bench>/<run-id>/
  manifest.json        # benchmark, config, source fingerprint, per-target info
  <table>.csv          # one row per measurement or raw sample
  summary/<table>.csv  # n and medians per target × dimensions
  figures/             # default figures: .pdf, .png, self-contained .py
```

`run` writes the summaries and default figures after the last target. Re-plot,
pool runs with identical configs and sources, or draw a custom figure:

```sh
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --targets ours,ext4,nfs
uv run python -m agent_fs_bench plot results/mdtest/<run-n1> results/mdtest/<run-n16> \
  --vary tasks --table phases --x tasks --y ops_per_s --split operation,layout \
  --yscale 1000 --logx
```

Columns, summaries and plot options: [results and analysis](docs/results-and-analysis.md).

## Reproducibility

- Archive the checkout, `uv.lock`, `local/` configs (with the launcher `CONFIG` of
  `agent_fs_bench/cluster.py`) and result directories. The source fingerprint covers
  uncommitted changes, except each module's `CONFIG` literal. A workload's `CONFIG`
  is recorded as `config`; the launcher `CONFIG` only as `config.launcher_sha256`, a
  hash of `mpiexec`, `mpiexec_args` and `env`. Neither replaces a source archive.
- `plot` pools runs only when their source fingerprints match and their `config`
  matches apart from keys given to `--vary`; `mode` and `launcher_sha256` are part of
  `config`. It does not
  compare tool versions (in each target's `info`), kernel, mount options or host
  environment; keep those consistent yourself. The manifest records each machine's
  probed facts (`nodes`) and every job's verified task placement.
- No result is cold-cache. Page cache, FUSE client cache and server cache are
  separate layers, and each workload keeps its own sync and cache semantics.
- Tasks are pinned to logical CPUs; resource limits (cgroups) for FUSE clients,
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
