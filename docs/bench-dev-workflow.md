# Development-workflow filesystem benchmarks

`dev-workflow` replays a real kernel development cycle in one disposable Linux
checkout. `mini-dev-workflow` replays the same search/read/edit/build/commit
shape against a tiny C project using a real Git worktree. Both are Python
standard-library workloads in `agent_fs_bench/benchmarks/`.

The project being edited is independent of the filesystem being measured. Mini
edits a tiny C program; kernel edits five historical patches under Linux's
`fs/overlayfs/`. `CONFIG_OVERLAY_FS=y` includes those source files in the build;
it does not mount OverlayFS. Neither workload installs or boots the built kernel,
requires an OverlayFS target, or performs checkpoints or snapshots. Put the
checkout/worktree on whichever externally prepared filesystem you want to measure.

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

Mini needs a compiler selected by its Makefile, libc development files, and all
of `data/mini-dev-workflow/`, including `source-repo.bundle`. The fixture uses
Make's built-in `cc` by default and honours inherited `CC`/`CFLAGS` under normal
make precedence. It needs no network, sudo, fixture regeneration, or shortened
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
repository containing the fixture's base and five commits, plus the compiler
and binutils selected by Kbuild, patch, bc, bison, flex, Perl, OpenSSL, and
OpenSSL/libelf development headers. Missing compiler tools fail in make rather
than in a separate Python toolchain resolver. The default source is
`https://github.com/torvalds/linux.git`; it needs network and substantial disk
space. A prepared local mirror avoids the network: set `CONFIG["linux_url"]` to
the mirror path in `agent_fs_bench/benchmarks/dev_workflow.py`.

### Upstream build-environment semantics

Both workflows follow [perf-eval at `bc32d083`](https://github.com/YoloFS/perf-eval/tree/bc32d083):
invoke `make` with the inherited environment. The runner does not turn environment
variables into command-line make assignments, clear build overrides, or choose a
compiler itself. The configured `-j` values remain this harness's experiment
settings; this change does not alter parallelism or workflow timing boundaries.

- Kernel Kbuild chooses its GCC/LLVM/cross tools. An environment-only `CC` can
  be overridden by the kernel Makefile, unlike mini's `CC ?=`. Normal make
  precedence, including inherited `MAKEFLAGS`, applies; the runner does not
  promote `CC` or `LLVM` to stronger assignments.
- Upstream does not explicitly set or record `KCFLAGS`. Kbuild still consumes
  inherited `KCFLAGS`, `KAFLAGS`, and related overrides. The runner inherits
  them without recording resolved values or changing their semantics.
- As upstream, the config fixture is copied to `.config`. An inherited
  `KCONFIG_CONFIG` can select another file, so that run may not use the fixture.
  This is deliberately preserved, not repaired by silently forcing a path.
- `ARCH` is inherited, not fixed or rejected by a Python architecture allowlist.
  The supplied fixture describes 64-bit x86; `olddefconfig` can change that
  selection under a different `ARCH`. Cross-architecture execution is not a
  claim that the same kernel workload was measured.

Runs aggregate globally; kernel configuration, host, and environment are not
recorded, and no automatic grouping or invalidation protects comparisons.
Neither the kernel config nor inherited make variables are saved. Keep builds
comparable by using a controlled build environment, leaving
`KCONFIG_CONFIG`/output-directory overrides unset, and checking the intended
configuration yourself before comparing runs. A successful Git tree check
proves the edit replay, not that the intended files were compiled.

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

**The supplied kernel config is 64-bit x86, not a forced runtime architecture.**
The runner requires Linux but leaves architecture and toolchain compatibility to
Kbuild, matching upstream environment inheritance. Mini's native ARM64 support
does not establish equivalence of kernel workloads across architectures.

### Kernel config fixture

`data/dev-workflow/tinyconfig-overlayfs.config` is regenerated in this repository
at the pinned base commit (`c2c54b5f34f6^`, Linux 6.12.0-rc7), so it is no longer
byte-identical to perf-eval's file of the same name; the series and command files
are still taken from upstream. Rebuild it with the workload's own sequence:

```sh
# at the resolved base commit
make ARCH=x86 tinyconfig
printf 'CONFIG_64BIT=y\nCONFIG_OVERLAY_FS=y\n' >> .config
make ARCH=x86 olddefconfig
cp .config data/dev-workflow/tinyconfig-overlayfs.config
```

`ARCH=x86` is required: `CONFIG_64BIT` only exists for `ARCH=x86`, and
`tinyconfig` (an `allnoconfig` variant) sets every visible boolean to `n`, which
selects `CONFIG_X86_32=y`. A file generated under `ARCH=i386` has no `64BIT`
line at all and is silently converted to 64-bit by `olddefconfig` on a 64-bit
host. Regenerate after any change of pinned base, and check that the result keeps
`CONFIG_64BIT=y`, `CONFIG_X86_64=y` and `CONFIG_OVERLAY_FS=y`. `olddefconfig`
is idempotent on the shipped file, so a 32-bit run means the environment
(`ARCH`, `KCONFIG_CONFIG`) overrode it, not that the fixture changed.

Compiler-probe symbols (`CONFIG_CC_VERSION_TEXT`, `CONFIG_*_VERSION`,
`CONFIG_CC_HAS_*`, `CONFIG_TOOLS_SUPPORT_RELR`) only record the toolchain that
generated the file; the measured run's `olddefconfig` recomputes them from the
toolchain it actually uses. Use one coherent `CC`/`LD`/`NM`/`OBJCOPY` set when
regenerating, since a mixed set can fail a probe and bake in a wrong default.

## Workload and timing boundaries

### Kernel

Fixtures are `data/dev-workflow/overlayfs-ovl-file.json`, its command files,
and `tinyconfig-overlayfs.config`. The base expression `c2c54b5f34f6^` is
resolved to a full commit ID before measurement.

1. **Preparation:** clone, verify repository/object availability, clean,
   detached checkout of the base, clean, check HEAD/cleanliness.
2. **Measured configuration:** `make tinyconfig`, copy the fixture to `.config`,
   `make olddefconfig`; these remain one configuration step. Every make call
   inherits the build environment without injected toolchain assignments.
3. **Measured work:** full build, then each commit's search/read/edit commands,
   incremental build, `git status`, `git diff --stat`, `git add`, `git commit`.
   Command-file loading is measured separately from command execution.
4. **Measured assertions:** after every commit, `HEAD^{tree}` must equal the
   fixture commit's tree. A mismatch fails the run.
5. **Untimed provenance:** query the compiler version through the actual
   Makefile's `$(CC)`. This happens after the workload timer, before cleanup.
6. **Cleanup:** `git reset --hard <base>` and `git clean -fdx`, separately timed.

The shipped fixture has five commits, 15 search + 10 read + 41 edit commands,
one full build and five incremental builds.
Build parallelism is `CONFIG["jobs"]` (default eight).

### Mini

The committed bundle and matching `data/mini-dev-workflow/series.json` preserve
real Git history. Source cloning is `source_setup_ms`, separate from
`preparation_ms` and the measured workload. Source validation checks commit
availability and cleanliness. **`git worktree prune` and `git worktree add`
remain measured**, followed by the initial build and both commits' complete
search/read/edit/incremental-build/status/diff/add/commit sequences. The
compiler version selected by make is collected after timing, before worktree
removal/pruning. As upstream, command exit codes are checked but commit trees
are not compared.

Mini defaults to two build jobs via `CONFIG["jobs"]` and never exceeds two; a
value of one is supported. The shipped fixture builds one C translation unit. A
complete run records 27 timed step entries. Its
source repository remains after cleanup. `tools/gen_mini_source_repo.py` is
fixture maintenance only, not a runtime helper; it rebuilds
`data/mini-dev-workflow/source-repo`, rewrites `series.json`, and exports the
committed bundle. Keep the bundle and matching series together.
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

The common envelope includes benchmark, run ID, target, case, repeat,
parameters, a harness source fingerprint, metrics and details. Parameters
include only the necessary experiment inputs:

- `jobs`: the effective build parallelism;
- `base_commit`: the resolved fixture base commit;
- `fixture_sha256`: SHA-256 hashes of the fixture series, command files and
  config/bundle inputs;
- `tool_versions`: concise Git, Make and actual compiler versions.

The compiler version is read through the makefile-selected `$(CC)`, not a
Python guess based on environment `CC`. Collection is outside workload and
cleanup timers. No full fixture body, kernel config, inherited build-variable
dictionary, resolved make variables, host, or environment record is stored.
Runs aggregate globally; environment consistency is the operator's
responsibility.

Numeric metrics are:

- `elapsed_ms`: total measured workflow interval, excluding setup/preparation
  and cleanup;
- `preparation_ms` and `cleanup_ms`: separately measured lifecycle phases;
- `unattributed_ms`: `max(0, elapsed_ms - sum(step ms))`;
- mini only: `source_setup_ms` for the bundle clone.

`details` retains only the complete ordered `steps` array (`step`, `ms`) and
kernel `tree_assertions` (`commit`, `tree`, `ok`). Durations are integer
milliseconds floored from a monotonic nanosecond clock. Compare measured totals
separately from preparation and cleanup; do not treat dependent steps as
independent samples. Fresh directories remove stale-owner recovery and cross-run
checkout reuse, but do not make reads cold.

## Durability, limitations and provenance

Git receives `core.fsync=committed` and `core.fsyncMethod=fsync`. Git commits
flush committed repository data; editing/building does not add explicit fsync.
No cache is dropped, so reads are warm. These are throughput/efficiency
measurements, **not crash-durability measurements**. No cgroup controls are
introduced.

Unlike upstream's backend-managed transitions, these workflows perform no
checkpoints or snapshots and emit no placeholder checkpoint steps. Fixture commands run under
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
