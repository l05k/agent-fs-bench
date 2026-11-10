# Development-workflow filesystem benchmarks

`dev-workflow` replays a real kernel development cycle in disposable Linux
checkouts. `mini-dev-workflow` replays the same search/read/edit/build/commit
shape against a tiny C project using real Git worktrees. Both are Python
standard-library workloads in `agent_fs_bench/benchmarks/`. One run launches
`tasks` independent instances at once; curves over the task count come from
separate runs pooled with `plot --vary tasks`.

The project being edited is independent of the filesystem being measured. Mini
edits a tiny C program; kernel edits five historical patches under Linux's
`fs/overlayfs/`. `CONFIG_OVERLAY_FS=y` includes those source files in the build;
it does not mount OverlayFS. Neither workload installs or boots the built kernel,
requires an OverlayFS target, or performs checkpoints or snapshots. Put the
checkout/worktree on whichever externally prepared filesystem you want to measure.

## Run

Mount filesystems externally before running; the benchmark never mounts or
unmounts them. Every machine of the pool keeps the same checkout and its own
`uv sync --locked` environment. Run from the controller's checkout:

```sh
uv sync --locked
uv run python -m agent_fs_bench run mini-dev-workflow --mode local --dirs /mnt/testfs
uv run python -m agent_fs_bench run dev-workflow --mode local --dirs /mnt/testfs
```

`--mode local` runs tasks on the controller; `overflow` deals tasks round-robin over
the controller and the machines in `local/hosts.toml` in file order; `remote` uses
only those machines and the controller hosts no task. Every mode launches one
Open MPI job with one task per allocated slot.

There are no workload tuning flags. The common runner selects targets and mode;
workload help accepts only `--help` and opens no repositories:

```sh
uv run python -m agent_fs_bench run dev-workflow -- --help
uv run python -m agent_fs_bench run mini-dev-workflow -- --help
```

Every task gets a fresh directory
`<target>/agent-fs-bench/<benchmark>/<run-id>/<node>/task-NNNN/`. Its `repo`
child is that task's kernel checkout or mini worktree. Mini's `repo-source`
sibling holds the Git metadata and objects, so **both source and worktree are on
the filesystem under test**. Each machine prepares its own data in its own
directories; no task reads a directory prepared by another machine. Kernel
cleanup resets/cleans each checkout; mini cleanup removes/prunes each worktree.
The kernel checkouts and mini sources remain available for inspection, but
subsequent runs never reuse them. Cleanup never deletes the target root.

## Configuration

Experiment settings are module-level `CONFIG` dictionaries near the top of each
workload; edit them in source for a deliberate experiment. Defaults are:

| `CONFIG` key | `dev-workflow` | `mini-dev-workflow` |
| --- | --- | --- |
| `fixture_dir` | `data/dev-workflow` | `data/mini-dev-workflow` |
| `require_fstype` | `None` | `None` |
| `tasks` | `1` | `1` |
| `jobs` | `8` | `2` (hard maximum 2) |
| `verbose` | `False` | `False` |
| `linux_url` | `https://github.com/torvalds/linux.git` | not used |

`fixture_dir` is relative to each machine's checkout and is validated there
before any preparation; an absolute path must exist on every machine. These are
source constants, not CLI flags; there are no compatibility aliases.
`require_fstype` is the expected `findmnt` filesystem type for the measured
checkout/worktree, for example `ext4`; `None` disables that check. `verbose`
shows command output. Kernel `linux_url` may be a Git URL or a local mirror path;
a local path is resolved on each machine, so use one that exists there.

`tasks` is the number of simultaneous independent instances, one per task.
`jobs` is the task's build parallelism, passed to `make -j`; tasks above a
machine's CPU count time-share its CPUs. Mini caps `jobs` at 2, matching the
harness's fixed build parallelism.

## Prerequisites and complete smoke commands

Both require Linux on every machine of the pool, Python 3.12+, Git supporting
worktrees and `core.fsync=committed`/`core.fsyncMethod=fsync`, Bash, Make, grep,
sed, `python3` on PATH, and a working compiler/linker. Python dependencies are
pinned by `uv.lock`: run `uv sync --locked` once per machine. Install system
prerequisites yourself. `findmnt` is required when `CONFIG["require_fstype"]` is
set. Tool presence and versions are checked on each node leader, the machine
that does the work.

Mini needs a compiler selected by its Makefile, libc development files, and all
of `data/mini-dev-workflow/`, including `source-repo.bundle`. The fixture uses
Make's built-in `cc` by default and honours inherited `CC`/`CFLAGS` under normal
make precedence. It needs no network, sudo, fixture regeneration, or shortened
smoke mode. This runs its **entire** two-commit workload:

```sh
uv sync --locked
mkdir -p /var/tmp/agent-fs-bench-smoke
uv run python -m agent_fs_bench run mini-dev-workflow --mode local \
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
space, and each task clones it. A prepared local mirror avoids the network: set
`CONFIG["linux_url"]` to the mirror path in
`agent_fs_bench/benchmarks/dev_workflow.py`; a relative path is resolved in each
machine's checkout.

### Upstream build-environment semantics

Both workflows follow [perf-eval at `bc32d083`](https://github.com/YoloFS/perf-eval/tree/bc32d083):
invoke `make` with the inherited environment. The runner does not turn environment
variables into command-line make assignments, clear build overrides, or choose a
compiler itself. The configured `-j` value is the task's build parallelism
(`jobs`) and remains this harness's experiment setting.

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

Runs aggregate globally; kernel configuration and environment are not recorded,
and no automatic grouping or invalidation protects comparisons. Each task's node
is recorded as the `host` column, but a pooled comparison still needs a
controlled environment. Neither the kernel config nor inherited make variables
are saved. Keep builds comparable by using a controlled build environment,
leaving `KCONFIG_CONFIG`/output-directory overrides unset, and checking the
intended configuration yourself before comparing runs. A successful Git tree
check proves the edit replay, not that the intended files were compiled.

Then run a complete replay:

```sh
uv sync --locked
mkdir -p /var/tmp/agent-fs-bench-smoke
uv run python -m agent_fs_bench run dev-workflow --mode local \
  --dirs /var/tmp/agent-fs-bench-smoke
```

This is a complete kernel replay, not a reduced smoke. Every task clones the
source with `--no-local` to avoid sharing source object files across filesystems.
Missing fixture commits fail immediately; no fallback fetch or alternate
workload runs.

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

`ARCH=x86` is required because `CONFIG_64BIT` only exists for that `ARCH`, and
`tinyconfig` (an `allnoconfig` variant) sets every visible boolean to `n`, which
selects `CONFIG_X86_32=y`; the `64BIT` line is therefore written explicitly
instead of being left to the default. The previous file came from perf-eval's
7.0.0-rc5 tree and pinned the 32-bit selection that `tinyconfig` produces, while
this workload builds `c2c54b5f34f6^` (Linux 6.12.0-rc7); regenerating at the
pinned base removes that version drift and the stale 32-bit pin together.
Regenerate after any change of pinned base, and check that the result keeps
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

1. **Per-task preparation (untimed):** clone, verify repository/object
   availability, clean, detached checkout of the base, clean, check
   HEAD/cleanliness. Each task clones into its own `task-NNNN/repo`.
2. **Measured configuration:** all instances meet at a barrier, then
   `make tinyconfig`, copy the fixture to `.config`, `make olddefconfig`; these
   remain one configuration step. Every make call inherits the build environment
   without injected toolchain assignments.
3. **Measured work:** full build, then each commit's search/read/edit commands,
   incremental build, `git status`, `git diff --stat`, `git add`, `git commit`.
   Command-file loading is measured separately from command execution.
4. **Measured assertions:** after every commit, `HEAD^{tree}` must equal the
   fixture commit's tree. A mismatch fails the run.
5. **Untimed provenance:** query the compiler version through the actual
   Makefile's `$(CC)`. This happens after the workload timer, before cleanup.
6. **Cleanup per task:** `git reset --hard <base>` and `git clean -fdx`,
   separately timed.

The shipped fixture has five commits, 15 search + 10 read + 41 edit commands,
one full build and five incremental builds per task.
Build parallelism is `CONFIG["jobs"]` (default eight), so each task's
`make -j8` runs eight build processes on its machine's CPUs.

### Mini

The committed bundle and matching `data/mini-dev-workflow/series.json` preserve
real Git history. Each task clones the bundle into its own `repo-source` beside
its `repo` worktree; cloning is `source_setup_ms`, separate from
`preparation_ms` and the measured workload. Source validation checks commit
availability and cleanliness. **`git worktree prune` and `git worktree add`
remain measured**, followed by the initial build and both commits' complete
search/read/edit/incremental-build/status/diff/add/commit sequences. The
compiler version selected by make is collected after timing, before worktree
removal/pruning. As upstream, command exit codes are checked but commit trees
are not compared.

Mini defaults to two build jobs via `CONFIG["jobs"]`, caps `jobs` at two, and
supports a value of one. The shipped fixture builds
one C translation unit. A complete run writes 27 rows per task to `steps.csv`.
Each task's source repository remains after cleanup.
`tools/gen_mini_source_repo.py` is fixture maintenance only, not a runtime
helper; it rebuilds `data/mini-dev-workflow/source-repo`, rewrites `series.json`,
and exports the committed bundle. Keep the bundle and matching series together.
Its fixture path is fixed in that tool, and its only option is `--force`:

```sh
python3 tools/gen_mini_source_repo.py --force
```

## Tables and figures

Every successful target writes one `workflow` row per task and one `steps` row
per task and measured step. Tables live next to `config.json` in
`results/<benchmark>/<run-id>/`; a run's targets share them, and the `target`
column identifies each row.

| table | key columns | metric columns |
| --- | --- | --- |
| `workflow` | `tasks`, `sample` (task index), `host` (node) | `elapsed_ms`, `preparation_ms`, `cleanup_ms`, `unattributed_ms`; mini also `source_setup_ms` |
| `steps` | `tasks`, `sample`, `host`, `step` (1-based order), `name`, `kind`, `commit` | `ms` |

`tasks` repeats the run's task count, `sample` is the task index and `host` is
the node name (`local` or the `hosts.toml` name). Both tables therefore have a
pooled sample index, so figures pool every task's rows. `kind` groups steps for
figures: `config`, `build`, `command`, `git` and `verify` for the kernel workload;
`worktree`, `build`, `command` and `git` for mini. `name` is the concrete
operation (`initial-build`, `search-load`, `search`, `commit`, ...). The `commit`
column holds the fixture commit for commit-specific steps and is empty otherwise.

`config.json` records the run configuration and per-target facts. Each node's
`targets[].info.hosts[<node>]` entry holds `fixture_sha256` and the Git, Make and
compiler versions of that machine. The target's `info` holds `jobs`,
`base_commit`, and (kernel workload) `tree_assertions`, the
verified tree hash of every replayed commit. All instances start together, so the
largest `elapsed_ms` of a run approximates its batch time. The compiler version is
read through the makefile-selected
`$(CC)`, not a Python guess based on environment `CC`. Collection is outside
workload and cleanup timers. No full fixture body, kernel config, inherited
build-variable dictionary, resolved make variables, or environment record is
stored. Runs aggregate globally; environment consistency is the operator's
responsibility.

Durations are integer milliseconds floored from a monotonic nanosecond clock.
Cross-machine clocks are never compared: each task reports its own durations and
the batch wall time comes from barriers. Compare measured totals separately from
preparation and cleanup; do not treat dependent steps as independent samples.
Fresh directories remove stale-owner recovery and cross-run checkout reuse, but
do not make reads cold.

The default figures are `figures/step-duration-tasks-<N>.pdf` (with `.png` and a
standalone `.py` script): a box plot of step durations by `kind`, one box per
target, split by `tasks`; and `figures/workflow-duration.pdf`: a box plot of
per-task workflow time at each `tasks` value, which stays meaningful when runs
with different task counts are pooled. `python -m agent_fs_bench plot
<run-dir>...` draws them from finished runs; pool runs with
`--vary tasks` and split or use `tasks` in custom figures.

Targets run sequentially, while a target's tasks run concurrently in one MPI
job. Any failing task raises, which aborts the whole job: the target emits no
`workflow` or `steps` rows, later targets are not attempted, and no row exists
to plot. Rows from earlier successful targets remain in the run directory.

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
