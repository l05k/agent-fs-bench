# agent-fs-bench

Filesystem benchmarks for agent workloads. Each workload runs against an
already-mounted directory, so local disks, FUSE and NFS share one interface
(`mount-scale` can also run real mount commands). Raw results are JSONL; each run
also writes a derived `summary.csv`. Figures are generated on demand.

Formal runs target Linux with Python 3.12+. The repository contains no internal
filesystem implementation or credentials. Any error stops the run; fix the
environment and rerun.

## Install

```sh
uv sync --locked                       # includes DuckDB
uv sync --locked --group analysis      # Matplotlib, only for plotting
uv run python -m agent_fs_bench --help
```

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
| `agent-replay` | Real Harbor/Docker tasks driven by accepted model-response cassettes | [agent replay](docs/bench-agent-replay.md) |

```sh
uv run python -m agent_fs_bench run <command> --dirs /mnt/fs-a
```

The only workload flags are `clone --smoke`, `mdtest --smoke` and
`mount-scale --directory-smoke` (pass them after `--`). All other settings
(scale, repeats, concurrency, PRAGMAs, tool versions, paths) live in the `CONFIG`
dict at the top of each module in `agent_fs_bench/benchmarks/`. Changing an
experiment means editing `CONFIG`; the source fingerprint in each result records
it. Keep private values out of commits.

`multi-host` and `mount-scale` read SSH hosts from `local/hosts.toml` (see
`experiments/hosts.example.toml`). Their sync and measurement protocols differ, so
do not combine their results. `mount-scale` also needs `local/mounts.toml`.

## Results

```text
results/<bench>/<run-id>/
  results.jsonl   # raw data, self-contained
  summary.csv     # mean/median/min/max/stddev per target × case × metric
```

`summary.csv` groups by `parameters` and `source_sha256`, so different configs or
source versions never merge silently. Regenerate it, query with DuckDB, or plot:

```sh
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --sql 'SELECT benchmark, "case", metric, value FROM metrics'

uv run --group analysis python -m agent_fs_bench plot 'results/clone/*/results.jsonl' \
  --benchmark clone --metric mib_s --targets ours,ext4,nfs --output results/clone
```

Schema, plot kinds and selectors: [results and analysis](docs/results-and-analysis.md).

## Reproducibility

- Archive the checkout, `uv.lock`, `local/` configs and JSONL. The source
  fingerprint covers uncommitted changes but does not replace a source archive.
- Kernel, mount options and host environment are not grouping keys; tool
  versions recorded in `parameters` are. Keep the former consistent yourself.
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
