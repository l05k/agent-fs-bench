# Development-workflow filesystem benchmarks

`dev-workflow` replays a real kernel development cycle in one disposable Linux
checkout. `mini-dev-workflow` replays the same search/read/edit/build/commit
shape against a tiny C project using a real Git worktree. Both are Python
standard-library workloads in `agent_fs_bench/benchmarks/`.

## Run

Mount filesystems externally before running; the benchmark never mounts or
unmounts them. Run from a checkout containing the package and `data/` fixtures:

```sh
uv sync --locked
uv run python -m agent_fs_bench run mini-dev-workflow --dirs /mnt/testfs
uv run python -m agent_fs_bench run dev-workflow --dirs /mnt/testfs
```

There are no workload tuning flags. The common runner selects targets; workload
help accepts only `--help` and opens no repositories:

```sh
uv run python -m agent_fs_bench run dev-workflow -- --help
uv run python -m agent_fs_bench run mini-dev-workflow -- --help
```

Every invocation gets a fresh directory
`<target>/agent-fs-bench/<benchmark>/<run-id>/`. Its `repo` child is the kernel
checkout or mini worktree. Mini's `repo-source` sibling holds Git metadata and
objects, so **both source and worktree are on the filesystem under test**.
Kernel cleanup resets/cleans its checkout; mini cleanup removes/prunes its
worktree. The kernel checkout and mini source remain available for inspection,
but subsequent runs never reuse them. Cleanup never deletes the target root.

## Configuration

Experiment settings are module-level `CONFIG` dictionaries near the top of each
workload; edit them in source for a deliberate experiment. Defaults are:

| `CONFIG` key | `dev-workflow` | `mini-dev-workflow` |
| --- | --- | --- |
| `fixture_dir` | `data/dev-workflow` | `data/mini-dev-workflow` |
| `require_fstype` | `None` | `None` |
| `jobs` | `8` | `2` (hard maximum 2) |
| `verbose` | `False` | `False` |
| `linux_url` | `https://github.com/torvalds/linux.git` | not used |

`fixture_dir` is an absolute, JSON-safe string derived from the project root and
is validated before any preparation. These are source constants, not CLI flags;
there are no compatibility aliases.
`require_fstype` is the expected `findmnt` filesystem type for the measured
checkout/worktree, for example `ext4`; `None` disables that check. `verbose`
shows command output. Kernel `linux_url` may be a Git URL or a full local
repository path.

## Prerequisites and complete smoke commands

Both require Linux, Python 3.12+, Git supporting worktrees and
`core.fsync=committed`/`core.fsyncMethod=fsync`, Bash, Make, grep, sed, `python3`
on PATH, and a working compiler/linker. Python dependencies are pinned by
`uv.lock`: run `uv sync --locked` once, which also installs DuckDB for the
default `summary.csv` export. Install system prerequisites yourself.
`findmnt` is required when `CONFIG["require_fstype"]` is set.

Mini needs a usable `cc` (or set `CC` to the desired compiler), libc development
files, and all of `data/mini-dev-workflow/`, including `source-repo.bundle`.
The fixture Makefile respects `CC`/`CFLAGS`; its default compiler is Make's
built-in `cc`. It needs no network, sudo, fixture regeneration, or shortened
smoke mode. This runs its **entire** two-commit workload:

```sh
uv sync --locked
mkdir -p /var/tmp/agent-fs-bench-smoke
uv run python -m agent_fs_bench run mini-dev-workflow \
  --dirs /var/tmp/agent-fs-bench-smoke
```

Inside a Linux VM, use a guest-local target rather than a host-shared project
path if testing the guest filesystem. Mini supports native ARM64 Linux.

Kernel additionally requires a full, non-partial, non-shallow, non-sparse Linux
repository containing the fixture's base and five commits, plus a C compiler
selected as described below, its matching binutils (`ld`, `ar`, `objcopy`,
`nm`, `strip`), patch, bc, bison, flex, Perl, OpenSSL, and OpenSSL/libelf
development headers. GCC is only the default fallback; LLVM and cross
toolchains are supported. The default source is
`https://github.com/torvalds/linux.git`; it needs network and substantial disk
space. A prepared local mirror avoids the network: set `CONFIG["linux_url"]` to
the mirror path in `agent_fs_bench/benchmarks/dev_workflow.py`.

Kernel toolchain selection mirrors the top-level kernel Makefile:

- With neither `CC` nor `LLVM` set, the compiler is
  `${CROSS_COMPILE}gcc` and binutils are prefixed by `CROSS_COMPILE`.
- `CC` overrides the compiler and may contain arguments or a wrapper, for
  example `CC="ccache clang"`. Its shlex-normalized value is passed as a
  command-line `CC=` assignment to every `make` invocation, because the kernel
  Makefile would otherwise replace an environment-only `CC`. `CROSS_COMPILE`
  still prefixes the non-LLVM binutils.
- A nonempty `LLVM` enables Kbuild's LLVM tools, and every invocation receives
  `LLVM=<value>`. Standard forms are `LLVM=1`, a tool directory ending in `/`
  (for example `LLVM=/opt/llvm/bin/`), and a version suffix such as `LLVM=-14`
  (selecting `clang-14`, `ld.lld-14`, and so on). As in Kbuild, any other
  nonempty bare value behaves like `LLVM=1`.
- If `CC` and `LLVM` are both set, `CC` selects the compiler while `LLVM`
  selects the remaining tools.
- Preflight checks the selected compiler and selected binutils rather than an
  unrelated host GCC, so an LLVM-only host needs no GCC for `LLVM=1`. With
  `CC=clang` and `LLVM` unset, Kbuild's host tools still follow its default
  `HOSTCC`; set `LLVM` for a fully GCC-free build.

The resolved compiler command, `LLVM` value, and make variable assignments are
recorded in portable parameters alongside the compiler version, and the same
assignments are used for configuration and every full/incremental build.

Then run a complete replay:

```sh
uv sync --locked
mkdir -p /var/tmp/agent-fs-bench-smoke
uv run python -m agent_fs_bench run dev-workflow \
  --dirs /var/tmp/agent-fs-bench-smoke
```

This is a complete kernel replay, not a reduced smoke. Local clones use
`--no-local` to avoid sharing source object files across filesystems. Missing
fixture commits fail immediately; no fallback fetch or alternate workload runs.

**The pinned kernel config is 32-bit x86.** Kernel runs require an x86 Linux
host and a toolchain able to build that config; ARM hosts and non-x86 `ARCH`
are rejected. This migration does not port the kernel fixture to ARM. Do not
use mini's ARM support as evidence that the kernel fixture supports ARM.

## Workload and timing boundaries

### Kernel

Fixtures are `data/dev-workflow/overlayfs-ovl-file.json`, its command files,
and `tinyconfig-overlayfs.config`. The base expression `c2c54b5f34f6^` is
resolved to a full commit ID before measurement.

1. **Preparation:** clone, verify repository/object availability, clean,
   detached checkout of the base, clean, check HEAD/cleanliness, count objects.
2. **Measured configuration:** `make tinyconfig`, copy the fixture config,
   `make olddefconfig`; these remain one configuration step. Every make
   invocation carries the same resolved toolchain assignments.
3. **Measured work:** full build, then each commit's search/read/edit commands,
   incremental build, `git status`, `git diff --stat`, `git add`, `git commit`.
   Command-file loading is measured separately from command execution.
4. **Measured assertions:** after every commit, `HEAD^{tree}` must equal the
   fixture commit's tree. A mismatch fails the run.
5. **Cleanup:** `git reset --hard <base>` and `git clean -fdx`, separately timed.

The shipped fixture has five commits, 15 search + 10 read + 41 edit commands,
53 zero-cost checkpoint markers, one full build and five incremental builds.
Build parallelism is `CONFIG["jobs"]` (default eight).

### Mini

The committed bundle and matching `data/mini-dev-workflow/series.json` preserve
real Git history. Source cloning is `source_setup_ms`, separate from
`preparation_ms` and the measured workload. Source validation checks commit
availability and cleanliness. **`git worktree prune` and `git worktree add`
remain measured**, followed by the initial build and both commits' complete
search/read/edit/incremental-build/status/diff/add/commit sequences. Worktree
removal/pruning is cleanup, outside the workload timer. As upstream, command
exit codes are checked but commit trees are not compared.

Mini defaults to two build jobs via `CONFIG["jobs"]` and never exceeds two; a
value of one is supported. The shipped fixture builds one C translation unit. A
complete run records 35 step entries, including eight zero-cost checkpoints. Its
source repository remains after cleanup with any loose objects produced by the
run. `tools/gen_mini_source_repo.py` is fixture maintenance only, not a runtime
helper; it rebuilds `data/mini-dev-workflow/source-repo`, rewrites `series.json`,
and exports the committed bundle. Keep the bundle and matching series together.
Its fixture path is fixed in that tool, and its only option is `--force`:

```sh
python3 tools/gen_mini_source_repo.py --force
```

## JSONL output

The sole raw result is
`results/<benchmark>/<run-id>/results.jsonl`.
Each successful target emits **one `case="workflow"` measurement**, not a row
per dependent step. No workload-specific `result.json`, summary, manifest, or
schema-version field is generated. The common runner derives `summary.csv`
only after every selected target succeeds. A failed target emits no successful
measurement; any earlier targets' completed JSONL rows remain available.

Targets run sequentially. A failing target reports its error, aborts the run,
and prevents later targets from being attempted; no measurement is emitted for
the failed target and no `summary.csv` is written. `summary.csv` is produced
only after **every selected target has completed successfully** and the JSONL
file has closed. An export failure returns a nonzero status but keeps the
completed JSONL; measurements are not rerun.

The common envelope includes benchmark, run ID/time, target, environment and
parameters. Parameters include the **effective jobs**, Git/Make/compiler
versions, the resolved toolchain (`source`, normalized compiler `command`,
`llvm`, and the make variable assignments passed to every invocation),
relevant build-environment overrides (including `CC`, `CROSS_COMPILE`, and
`LLVM`), the complete fixture series, and SHA-256 hashes of every selected
fixture input (including mini's bundle or kernel's config). Exported JSONL
therefore carries fixture identity and provenance without relying on a
host-local manifest.

Numeric metrics are:

- `elapsed_ms`: total measured workflow interval, excluding setup/preparation
  and cleanup;
- `preparation_ms` and `cleanup_ms`: separately measured lifecycle phases;
- `unattributed_ms`: `max(0, elapsed_ms - sum(non-checkpoint step ms))`;
- mini only: `source_setup_ms` for the bundle clone.

`details` retains the complete ordered `steps` array (`step`, `ms`), zero-cost
checkpoint boundaries/count, kernel `tree_assertions`, resolved base commit,
filesystem requirement/detection, paths/actions, cleanup outcome, loose-object
counts, and `fsync_mode`/`cache_state`. Durations are integer milliseconds
floored from a monotonic nanosecond clock. Compare measured totals separately
from preparation and cleanup; do not treat dependent steps as independent
samples. Fresh directories remove stale-owner recovery and cross-run checkout
reuse, but do not make reads cold.

## Durability, limitations and provenance

Git receives `core.fsync=committed` and `core.fsyncMethod=fsync`. Git commits
flush committed repository data; editing/building does not add explicit fsync.
No cache is dropped, so reads are warm. These are throughput/efficiency
measurements, **not crash-durability measurements**. No cgroup controls are
introduced.

Checkpoint markers are boundaries only, not backend snapshots; results are
not comparable to backend-checkpoint timings. Fixture commands run under
`bash --noprofile --norc -c` with `LC_ALL=C`, without `pipefail`. Hooks are
disabled and commits use a fixed benchmark identity. Process-group
cancellation and out-of-band cleanup remain in place.

Workflow shape and fixtures derive from
[YoloFS/perf-eval](https://github.com/YoloFS/perf-eval), including
`src/workloads/mini_dev_workflow.rs` at `bc32d083`. The kernel series replays
Amir Goldstein's overlayfs `ovl_file` refactoring merged in Linux v6.13.
Default Linux history comes from
[torvalds/linux](https://github.com/torvalds/linux). Mini's deleted upstream
submodule history is reconstructed by the fixture-maintenance generator;
runtime uses the committed bundle rather than copying the reference tree.
