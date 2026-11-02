# agent-fs-bench

Benchmarking agent workloads on filesystems.

Three standards-library-only benchmarks, each a standalone script that can be
copied to a test machine and run with a plain `python3`:

| Script | Workload |
| --- | --- |
| [`dev_workflow_fs.py`](./dev_workflow_fs.py) | Kernel dev workflow (config/build/search/edit/commit) in one disposable Linux checkout |
| [`npm-install-fs-bench.py`](./npm-install-fs-bench.py) | Warm npm/pnpm cache, cold `node_modules` install |
| [`openclaw_session_bench.py`](./openclaw_session_bench.py) | OpenClaw transcript SQLite write microbenchmark |

## Setup

The project is managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync          # create .venv with the pinned Python
uv run <script>  # run any benchmark in the project environment
```

`pyproject.toml` declares no third-party dependencies and requires Python 3.12.
`.python-version` pins the interpreter; `uv.lock` is committed.

## Running

```sh
uv run dev_workflow_fs.py --repo /mnt/testfs/linux
uv run npm-install-fs-bench.py --dir /mnt/testfs --top-k 30 --runs 3
uv run openclaw_session_bench.py --dir /mnt/testfs --duration 30
```

`--repo` / `--dir` must point at the filesystem under test. Intermediate
artifacts (Linux checkout, npm/pnpm caches and `node_modules`, SQLite database
and WAL) stay on that filesystem so the measurements reflect it.

`dev_workflow_fs.py` reads its fixtures from `data/dev-workflow/` next to the
script (override with `--fixture-dir`); copy the script together with `data/`
to run it on another machine. It requires Linux, and its fixture commits must
exist in the `--repo` checkout or its `--linux-url` remote.

## Fixtures

```text
data/
  dev-workflow/          # used by dev_workflow_fs.py
  mini-dev-workflow/     # smaller variant for future scripts
```

These are copies of the upstream `fixtures/` directory, so the benchmark
scripts do not depend on an external checkout of that repository.

## Output layout

Only the profiling/timing results go to the project directory, under
`results/<script-name>/<UTC-run-id>/`, where the run id looks like
`20261102T042113Z`:

```text
results/
  dev_workflow_fs/20261102T042113Z/result.json
  npm-install-fs-bench/20261102T042113Z/
    results.csv
    summary.csv
    selected-packages.txt
    top-packages.csv        # only when the catalog is downloaded
    manifest.txt
    logs/{npm,pnpm}-run-<n>.log
  openclaw_session_bench/20261102T042113Z/
    result.json
    report.md
    report.md.tmpl
    summary.txt
    manifest.txt
```

The path is derived from each script's own location, so the current working
directory does not matter. There is no option to redirect it: a run started
within the same second reuses (and overwrites) that second's directory.

`results/` is git-ignored.

## Shared results helper

The `results/<script>/<timestamp>` logic lives in
[`tools/results_core.py`](./tools/results_core.py) and is *inlined* into each
script between generated markers, so the scripts keep no imports outside the
standard library and stay individually copyable. After editing the helper,
regenerate the copies:

```sh
uv run tools/sync_results_core.py          # rewrite the embedded copies
uv run tools/sync_results_core.py --check  # freshness check (exit 1 if stale)
```
