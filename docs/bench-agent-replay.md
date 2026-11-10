# Agent replay

Runs real Harbor/Docker tasks driven by a fixed, previously accepted sequence of
model responses. Only the task data directories live on the target filesystem.

**Scope:** this replays model responses, not syscalls, and does not measure an
online adaptive agent. The agent, shell commands, verifier and artifact
collection still run for real, so I/O may differ between runs.

Each run measures **one** case, **one** repeat count and **one** external
concurrency (`tasks`). Every task replays every repeat of that case independently
on its own machine, and one global Open MPI job starts all tasks together. Curves
over tasks, and comparisons of cases, come from separate runs pooled with
`plot --vary tasks` or `--vary case`.

## Requirements

- The [agent-mitm-replay](https://github.com/l05k/agent-mitm-replay) submodule,
  pinned at `33c73261c8b656bf11a1915a75e56a253dee6bf0`, with its own `uv.lock`.
  It needs Python 3.13.x, Harbor 0.24.0 and mitmproxy 12.2.3. These do not enter
  the main project's dependencies.
- Linux with Docker and Compose v2 on **every** participating machine. The Docker
  daemon must run on that machine and see its target mount. On macOS, run the
  whole benchmark inside `colima ssh`; pointing Docker at the VM while using
  host-only paths does not work.

```sh
git submodule update --init --recursive   # public HTTPS, no credentials
uv sync --locked --project third_party/agent-mitm-replay
```

Run both commands in every machine's checkout. The controller uses them only if
it hosts tasks.

## Prepare tasks and cassettes

The repository ships no task set or model credentials. You provide a single-step
Harbor task, a pinned container image, the initial task data, and an accepted
cassette. Bake tools and dependencies into the image and pin its digest, so image
pulls, builds and online downloads stay out of measured samples. The task source
hash does not cover the image; archive the image and host environment separately.

Each task is recorded once, validated once on a fresh copy of the initial data,
and the accepted cassette is then reused for every repeat. Validation checks task
success, full response consumption and an identical command trace. Formal replays
do not re-check reward, command trace or leftover responses. Do not compare
record/validate time with replay time.

```sh
# Paths are relative to the repo root; initial data must be a pristine snapshot
mkdir -p local/replay/reference
cp -a local/replay/initial/task-a local/replay/reference/task-a-record

# OPENAI_API_KEY is needed only for record; set it in the environment
uv run --project third_party/agent-mitm-replay agent-mitm-replay record \
  --task "$PWD/local/replay/task-a" \
  --cassette "$PWD/local/replay/cassettes/task-a" \
  --model openai/gpt-4o-mini --upstream https://api.openai.com \
  --mount "$PWD/local/replay/reference/task-a-record:/app"

# Validate on a fresh copy; this is not a performance sample
cp -a local/replay/initial/task-a local/replay/reference/task-a-validation
uv run --project third_party/agent-mitm-replay agent-mitm-replay validate \
  --task "$PWD/local/replay/task-a" \
  --cassette "$PWD/local/replay/cassettes/task-a" \
  --mount "$PWD/local/replay/reference/task-a-validation:/app"

cp experiments/agent-replay.example.toml local/agent-replay.toml
```

In `local/agent-replay.toml`, each `[[cases]]` sets `name`, `task` and
`cassette`; each `[[cases.mounts]]` sets the initial data `source` and an
absolute `container` path. Paths are relative to the TOML file or absolute.
Multiple cases and multiple non-nested mounts are allowed. Container paths must
match those used for record/validate. The initial data must contain everything
the bind mount hides in the image, including `.git`, hidden files and links. Use
self-contained snapshots: no symlinks or Git alternates pointing outside them.

Every machine reads its own checkout's cases file. Keep the file at the same
repo-relative path (`local/agent-replay.toml`) in every checkout, and make every
path it references — task, cassette, each initial-data `source` — exist on every
participating machine. Relative paths resolve against the TOML file's directory,
so the same relative layout in each checkout is enough. Docker images must
already be present on every machine. Keep tasks, cassettes, initial data, the
submodule environment, Docker images and Harbor logs on a local disk of that
machine, outside the target. The run fails before any measurement when an input
is missing, inside the target, or fingerprinted differently on another machine.

## Run

```sh
# Local: every task on this machine
uv run python -m agent_fs_bench run agent-replay --dirs /mnt/fs-a --mode local

# Several machines: one global job, tasks spread over the probed machines
uv run python -m agent_fs_bench run agent-replay --dirs /mnt/fs-a --mode overflow
```

`--mode local` runs every task on the controller; `overflow` deals tasks round-robin
over the controller and the machines in `local/hosts.toml` in file order; `remote` uses
only those machines and hosts no task on the controller. Tasks above a machine's CPU
count share CPUs.

`CONFIG` in `agent_fs_bench/benchmarks/agent_replay.py` holds all settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `cases_file` | `local/agent-replay.toml` | Cases file, resolved in each machine's checkout. |
| `case` | `None` | Name of the one case to replay; `None` requires the cases file to list exactly one. |
| `repeats` | `5` | Independent replays per task. |
| `tasks` | `1` | Simultaneous independent replays, one per task process. |

`tasks` is the only concurrency control; there is no in-run sweep of cases,
repeats or tasks. Several tasks may share one machine: the replay starts
mitmdump on an ephemeral loopback port, requires a fresh output directory, and
Harbor names each Compose project after a per-trial random name, so containers,
networks, volumes and proxy state of simultaneous replays do not collide. A task
whose compose file pins `container_name` or a fixed host port opts out of that
namespace and cannot be replayed concurrently on one machine. The task image tag
is content-addressed and shared, so pull or build the image before measuring
instead of racing a first build. Mitmproxy creates its own CA in the per-replay
confdir, which costs startup time outside the measured window.

## Data preparation and timing

Each target replays the selected case; its repeats run in order. The node leader
checks the replay executable, the Python/Harbor/mitmproxy versions and every
input fingerprint of that case once per node, before any measurement, and records
them in `config.json`. The facts of all nodes must agree.

Before every repeat, each task restores all inputs into a fresh
`<target>/agent-fs-bench/agent-replay/<run-id>/<node>/task-NNNN/<case>/repeat-N/mount-I`
with GNU `cp -a --reflink=never`. The copy is fingerprinted and compared with the
node leader's source fingerprint (`initial_sha256`). Any mismatch in content,
mode, symlinks or hard links stops the run before replay. Fingerprints ignore
timestamps, owner, xattrs and ACLs. Restored copies are kept for inspection, so
one machine needs about tasks-per-node × repeats × total initial-data size.

All tasks restore and then replay the same case and repeat together, between two
barriers. Restore and fingerprinting are untimed and warm the cache, so results
are not cold-cache.

Replay output and logs are diagnostics and stay in that node's checkout, at
`results/agent-replay/<run-id>/<target>/hosts/<node>/task-NNNN/<case>/repeat-N/`
and `.../repeat-N.log`. They are never written to the target or to the result
tables.

## Tables and figures

`results/agent-replay/<run-id>/` holds one `config.json` plus flat CSV tables:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `replays` | `case`, `tasks`, `repeat`, `sample`, `host` | `elapsed_s`, `model_requests` |
| `phases` | `case`, `tasks`, `repeat`, `sample`, `host`, `phase` | `seconds` |

A `replays` row is one `target × case × repeat × task`; `sample` is the task
index and `host` the node it ran on. A `phases` row is one phase Harbor reported
for that replay. Both tables have a `repeat` column, and figures show the median
and IQR of the raw rows.

`plot` writes `figures/replay-time-tasks-<case>.pdf` (box plot of `elapsed_s` per
tasks value) and one `replay-phases-<case>-<tasks>.pdf` per case and tasks value
(box plot of `seconds` per Harbor phase; punctuation in the value becomes `_`).
Pooled runs need `plot --vary tasks` (and `case` or any other key that differs).

Per-node facts — Python/Harbor/mitmproxy versions, the submodule source hash, the
model name, and the SHA-256 of the task, cassette and each initial mount — are in
`config.json` under `targets[i].info.hosts[<node>]`. Response bodies are never
written to the tables.

## Metrics

| Metric | Boundary |
| --- | --- |
| `elapsed_s` (`replays`) | Upstream `elapsed_sec`: Harbor subprocess start to exit, including environment setup/cleanup, agent, verifier, artifacts and Harbor's own output |
| `seconds` (`phases`) | Phase time reported by Harbor in `phase_seconds`; a phase Harbor does not report has no row, it is not zero |
| `model_requests` (`replays`) | Model requests received during this replay; diagnostic only, not proof of trace equivalence |

`elapsed_s` excludes input restore, fingerprinting, proxy start/stop, runner
post-processing and CSV writes. Durations are per task on its own machine and are
never compared across machines; the barrier-based batch wall time is logged, not
tabulated. The verifier does real I/O and is timed even when reward is zero.
Model calls have no inference latency, but fixed waits in the recorded commands
remain. Terminal output, shell branches, background jobs and slow filesystems can
change real I/O, so do not claim identical syscalls or task correctness on every
target.

## Data confidentiality

A cassette's `flows.mitm` can contain provider API keys, full request headers,
task files and model output. Keep tasks, cassettes and configs under the ignored
`local/`. `.gitignore` excludes `*.mitm` and every `cassettes/` directory, but it
does not untrack files already committed. Review and redact before publishing any
artifact. Offline replay needs no API key and never falls back to the real provider.

## Token-free smoke test

```sh
uv run --project third_party/agent-mitm-replay agent-mitm-replay smoke \
  --output "$PWD/local/replay-smoke"
```

The upstream smoke uses a local scripted model and runs record → validate →
replay in real Docker. It checks a 20-byte artifact, rejection of unaccepted
cassettes, offline operation after the provider stops, and measurement when
verifier reward is zero. The output directory must not exist, and the first run
needs registry and apt network access. To exercise the main benchmark, point
`local/agent-replay.toml` at the generated `task`/`cassette`, use an empty
directory as `/app` initial data, and run:

```sh
uv run python -m agent_fs_bench run agent-replay --dirs /mnt/fs-a --mode local
```

The generated cassette is small, so it also runs cross-machine: copy the cases
file, task, cassette and initial data to each machine's checkout, sync the
submodule environment there, and use `--mode overflow` or `--mode remote`. This
toy task is not a paper workload.
