# agent-fs-bench

Benchmarking agent workloads on filesystems.

Five benchmarks. Each is a standalone script that can be copied alone to a test
machine and run with a plain system `python3`; four need only the standard
library, and the fifth shells out to SQLite's own C test program, which it
downloads and builds on the test machine:

| Script | Workload |
| --- | --- |
| [`dev_workflow_fs.py`](./dev_workflow_fs.py) | Kernel dev workflow (config/build/search/edit/commit) in one disposable Linux checkout |
| [`mini_dev_workflow_fs.py`](./mini_dev_workflow_fs.py) | Same workflow shape against a tiny C project in a disposable Git worktree; the fast reproducer |
| [`npm_install_fs_bench.py`](./npm_install_fs_bench.py) | Warm npm/pnpm cache, cold `node_modules` install, same- or cross-filesystem cache placement |
| [`openclaw_session_bench.py`](./openclaw_session_bench.py) | OpenClaw transcript SQLite write microbenchmark |
| [`kvtest_openclaw_fs.py`](./kvtest_openclaw_fs.py) | SQLite's official `kvtest --multitrans`: one commit per write, as an OpenClaw-shaped lower bound |

Per-benchmark reference notes (EN/中文) live in
[`docs/`](./docs):

- [`docs/bench-dev-workflow.md`](./docs/bench-dev-workflow.md)
- [`docs/bench-npm-install.md`](./docs/bench-npm-install.md)
- [`docs/bench-openclaw-session.md`](./docs/bench-openclaw-session.md)
- [`docs/bench-kvtest-openclaw.md`](./docs/bench-kvtest-openclaw.md)

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
uv run mini_dev_workflow_fs.py --repo /mnt/testfs/mini-worktree
uv run npm_install_fs_bench.py --dir /mnt/testfs --top-k 30 --runs 3
uv run npm_install_fs_bench.py --dir /mnt/nas/mount \
  --prep-dir /var/tmp/npm-prep --top-k 30 --runs 3
uv run npm_install_fs_bench.py --dir /mnt/testfs --top-k 30 --concurrency 8
uv run openclaw_session_bench.py --dir /mnt/testfs --duration 30
uv run kvtest_openclaw_fs.py --dir /mnt/testfs --count 2000
```

`--repo` / `--dir` must point at the filesystem under test. Intermediate
artifacts (Linux checkout, npm/pnpm caches and `node_modules`, SQLite database
and WAL) stay on that filesystem so the measurements reflect it.

`npm_install_fs_bench.py` is the exception: `--prep-dir` deliberately moves its
npm cache, pnpm store, pnpm XDG cache and seed projects to another filesystem,
so that every write timed in `--dir` is real data rather than a hardlink or a
server-side clone. Without `--prep-dir` the caches share `--dir`, which is the
historical behaviour. Its `--concurrency N` pins the parallelism of the timed
install (default `4`; `0` leaves each tool at its own default) on the knobs that
measurably matter — `UV_THREADPOOL_SIZE` for npm, `RAYON_NUM_THREADS` and
`PNPM_MACOS_LINK_THREADS` for pnpm. See
[`docs/bench-npm-install.md`](./docs/bench-npm-install.md#concurrency--并发).

`dev_workflow_fs.py` reads its fixtures from `data/dev-workflow/` next to the
script (override with `--fixture-dir`); copy the script together with `data/`
to run it on another machine. It requires Linux, and its fixture commits must
exist in the `--repo` checkout or its `--linux-url` remote.

`mini_dev_workflow_fs.py` also requires Linux. It creates a detached Git
worktree at `<fixture>/source-repo` into `--repo`, which must be absent or
empty, and removes it afterwards. The source repository is recreated from the
committed `source-repo.bundle` on first use if it is missing.

`kvtest_openclaw_fs.py` is the only benchmark that needs a C compiler. It is
still a single self-contained file: copy it alone to the test machine and run
it with the system `python3`. The first run downloads the SQLite 3.54.1
amalgamation and SQLite's `kvtest.c` into a `build/` directory next to the
script (git-ignored) and compiles `build/kvtest`; later runs reuse both. Set
`CC` to choose the compiler, or `--build-dir` to move the cache. See
[`docs/bench-kvtest-openclaw.md`](./docs/bench-kvtest-openclaw.md) for the
parameter comparison against OpenClaw and the gaps it cannot close.

## Fixtures

```text
data/
  dev-workflow/          # used by dev_workflow_fs.py
  mini-dev-workflow/     # used by mini_dev_workflow_fs.py
    source-repo.bundle   # committed Git bundle with the fixture history
    source-repo/         # extracted from the bundle; git-ignored
    repo/                # reference copy of the final tree (vendored)
```

These are copies of the upstream `fixtures/` directory, so the benchmark
scripts do not depend on an external checkout of that repository. Their origin
is recorded under [Credits and dependencies](#credits-and-dependencies).

Upstream kept `mini-dev-workflow/source-repo` as a Git submodule and later
deleted it, so the fixture repository is not vendored verbatim. It is rebuilt
deterministically from the checked-in edit commands:

```sh
uv run tools/gen_mini_source_repo.py --force   # rewrites source-repo/, bundle, series.json
```

The tool writes the base tree (`hello v1`), replays each `*-edit.cmds`, commits
it, asserts the result matches `mini-dev-workflow/repo/`, and re-exports the
bundle. Identities and dates are pinned, so repeated runs produce the same
commit ids.

## Output layout

Only the profiling/timing results go to the project directory, under
`results/<script-name>/<UTC-run-id>/`, where the run id looks like
`20261102T042113Z`:

```text
results/
  dev_workflow_fs/20261102T042113Z/result.json
  mini_dev_workflow_fs/20261102T042113Z/result.json
  npm_install_fs_bench/20261102T042113Z/
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
  kvtest_openclaw_fs/20261102T042113Z/
    result.json
    summary.txt
    manifest.txt
    kvtest-stdout.txt
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

## Credits and dependencies

The scripts have no third-party Python dependencies, but the workloads and
fixtures are derived from external work:

| Component | Source | Used by |
| --- | --- | --- |
| `dev-workflow` fixtures, and the base of `dev_workflow_fs.py` | [`YoloFS/perf-eval`](https://github.com/YoloFS/perf-eval) — `fixtures/` and `scripts/dev_workflow_fs.py` | `dev_workflow_fs.py` |
| `mini-dev-workflow` fixtures, and the base of `mini_dev_workflow_fs.py` | [`YoloFS/perf-eval`](https://github.com/YoloFS/perf-eval) — `fixtures/mini-dev-workflow` and `src/workloads/mini_dev_workflow.rs` | `mini_dev_workflow_fs.py` |
| Dev-workflow patchset | Amir Goldstein's overlayfs `ovl_file` refactoring, merged in Linux v6.13 (`fs/overlayfs/file.c`), replayed from upstream git history | `dev_workflow_fs.py` |
| Linux source tree | [`torvalds/linux`](https://github.com/torvalds/linux) (default `--linux-url`) | `dev_workflow_fs.py` |
| Top-package catalog | [gist `l05k/0c7dfaedb703fa6d5465e00dd297cdcd`](https://gist.github.com/l05k/0c7dfaedb703fa6d5465e00dd297cdcd) | `npm_install_fs_bench.py` |
| OpenClaw transcript schema and WAL settings | [`openclaw/openclaw`](https://github.com/openclaw/openclaw) at `5f6f15fed0d6` | `openclaw_session_bench.py` |

Vendored fixtures were copied verbatim from `YoloFS/perf-eval` at commit
`bc32d08` (2026-08-28), where `fixtures/dev-workflow` was last changed in
`de6f95f` and `fixtures/mini-dev-workflow` in `afe112a`. To refresh them:

```sh
git clone https://github.com/YoloFS/perf-eval /tmp/perf-eval
rm -rf data/dev-workflow data/mini-dev-workflow
cp -R /tmp/perf-eval/fixtures/dev-workflow /tmp/perf-eval/fixtures/mini-dev-workflow data/
```

Upstream `YoloFS/perf-eval` does not currently ship a `LICENSE` file, so no
explicit license is granted for those fixtures; confirm the terms with the
upstream authors before redistributing this repository. The Linux kernel
history the patchset replays is GPL-2.0, and OpenClaw is used only as a
schema/write-path reference, without copying its code.
