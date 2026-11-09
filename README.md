# agent-fs-bench

Filesystem benchmarks for agent workloads. Each workload runs against an
already-mounted directory, so local disks, FUSE and NFS share one interface
(`mount-scale` can also run real mount commands). Each run writes a manifest and
flat CSV tables, then per-group medians and paper-style figures.

Formal runs target Linux with Python 3.12+. The repository contains no internal
filesystem implementation or credentials. Any error stops the run; fix the
environment and rerun.

## Install

```sh
uv sync --locked
uv run python -m agent_fs_bench --help
```

Figures need the Arial font (Ubuntu: `sudo apt install ttf-mscorefonts-installer`).
Without it, `run` fails after the CSV tables are written; re-plot them elsewhere.

Deploy the whole checkout, including `data/`. External tools are prepared by the
experimenter; each workload doc lists its requirements. `agent-replay` needs a
submodule; see its doc.

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

Targets run sequentially. Working data goes to
`<target>/agent-fs-bench/<bench>/<run-id>/`; results go to
`results/<bench>/<run-id>/`.

## Workloads

| Command | Workload | Doc |
| --- | --- | --- |
| `dev-workflow` | Linux kernel configure, build, search, edit, commit, tree check | [dev workflow](docs/bench-dev-workflow.md) |
| `mini-dev-workflow` | Same Git worktree workflow on a small C project | [dev workflow](docs/bench-dev-workflow.md) |
| `npm-install` | Offline npm/pnpm install into cold `node_modules`, by concurrency batch | [npm install](docs/bench-npm-install.md) |
| `npm-install-nofsync` | Same, with no harness fsync; install timing, throughput and tree stats, no flush/durable metrics | [npm install](docs/bench-npm-install.md#no-fsync-variant) |
| `clone` | Durable same-filesystem directory clone; size, file-count and concurrency sweeps | [clone](docs/bench-clone.md) |
| `kvtest` | SQLite `kvtest` random BLOB overwrites, per-write or batched commits | [kvtest](docs/bench-kvtest-openclaw.md) |
| `sqlite-speedtest` | SQLite `speedtest1 main` SQL phases | [sqlite-speedtest](docs/bench-sqlite-speedtest.md) |
| `multi-host` | Concurrent private-tree ops and directory contention from several SSH hosts | [multi-host](docs/bench-multi-host-nas.md) |
| `mount-scale` | N independent clients (local cores first, then SSH hosts); Open MPI mdtest and mount lifecycle | [mount scale](docs/bench-mount-scale.md) |
| `mdtest` | Single-client POSIX metadata throughput; process-count sweep | [mdtest](docs/bench-mdtest.md) |
| `mdtest-peak` | Single-client peak sweep to 2× CPUs: file create/stat/read/unlink ops/s and MiB/s for 0 B–1 MiB files | [mdtest peak](docs/bench-mdtest-peak.md) |
| `agent-replay` | Real Harbor/Docker tasks driven by accepted model-response cassettes | [agent replay](docs/bench-agent-replay.md) |

```sh
uv run python -m agent_fs_bench run <command> --dirs /mnt/fs-a
```

The only workload flags are `clone --smoke`, `mdtest --smoke`, `mdtest-peak --smoke` and
`mount-scale --directory-smoke` (pass them after `--`). All other settings
(scale, repeats, concurrency, PRAGMAs, tool versions, paths) live in the `CONFIG`
dict at the top of each module in `agent_fs_bench/benchmarks/`. Changing an
experiment means editing `CONFIG`; each run's manifest records the resulting
`config` and a source fingerprint. Keep private values out of commits.

`multi-host` and `mount-scale` read SSH hosts from `local/hosts.toml` (see
`experiments/hosts.example.toml`). Their sync and measurement protocols differ, so
do not combine their results. `mount-scale` also needs `local/mounts.toml`.

## Results

```text
results/<bench>/<run-id>/
  manifest.json        # benchmark, config, source fingerprint, per-target info
  <table>.csv          # one row per measurement or raw sample
  summary/<table>.csv  # n and medians per target × dimensions
  figures/             # default figures: .pdf, .png, self-contained .py
```

`run` writes the summaries and default figures after the last target. Re-plot,
pool runs with identical configs, or draw a custom figure:

```sh
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --targets ours,ext4,nfs
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --table phases \
  --x procs --y ops_per_s --split operation,layout --yscale 1000 --logx
```

Columns, summaries and plot options: [results and analysis](docs/results-and-analysis.md).

## Reproducibility

- Archive the checkout, `uv.lock`, `local/` configs and result directories. The
  source fingerprint covers uncommitted changes but does not replace a source archive.
- `plot` pools runs only when their `config` matches, apart from keys given to
  `--vary`. It does not compare tool versions (in each target's `info`), kernel,
  mount options or host environment; keep those consistent yourself.
- No result is cold-cache. Page cache, FUSE client cache and server cache are
  separate layers, and each workload keeps its own sync and cache semantics.
- Resource limits (cgroups) for FUSE clients, servers and the benchmark are set
  externally.
- Smoke runs only prove the pipeline works; they are not performance or
  consistency results.

```sh
uv run python -m unittest discover -s tests
```

## Layout

```text
agent_fs_bench/   CLI, analysis, plotting; benchmarks/ holds one module per workload
experiments/      example TOML configs (copy to local/, which is git-ignored)
data/             fixed inputs, lockfiles, Git bundles (see docs/fixtures.md)
third_party/      agent-mitm-replay submodule
tools/            fixture maintenance
docs/             per-workload configuration and timing boundaries
```
