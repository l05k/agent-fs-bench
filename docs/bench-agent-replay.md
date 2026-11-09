# Agent replay

Runs real Harbor/Docker tasks driven by a fixed, previously accepted sequence of
model responses. Only the task data directories live on the target filesystem.

**Scope:** this replays model responses, not syscalls, and does not measure an
online adaptive agent. The agent, shell commands, verifier and artifact
collection still run for real, so I/O may differ between runs.

## Requirements

- The [agent-mitm-replay](https://github.com/l05k/agent-mitm-replay) submodule,
  pinned at `33c73261c8b656bf11a1915a75e56a253dee6bf0`, with its own `uv.lock`.
  It needs Python 3.13.x, Harbor 0.24.0 and mitmproxy 12.2.3. These do not enter
  the main project's dependencies.
- Linux with Docker and Compose v2. The Docker daemon must run on the same host
  and see the target mounts. On macOS, run the whole benchmark inside
  `colima ssh`; pointing Docker at the VM while using host-only paths does not work.

```sh
git submodule update --init --recursive   # public HTTPS, no credentials
uv sync --locked --project third_party/agent-mitm-replay
```

## Prepare tasks and cassettes

The repository ships no task set or model credentials. You provide a single-step
Harbor task, a pinned container image, the initial task data, and an accepted
cassette. Bake tools and dependencies into the image and pin its digest, so image
pulls, builds and online downloads stay out of measured samples. The task source
hash does not cover the image; archive the image and host environment separately.

Each task is recorded once, validated once on a fresh copy of the initial data,
and the accepted cassette is then reused for every filesystem and repeat.
Validation checks task success, full response consumption and an identical
command trace. Formal replays do not re-check reward, command trace or leftover
responses. Do not compare record/validate time with replay time.

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

Keep tasks, cassettes, initial data, the submodule environment, Docker images and
Harbor logs on a local control disk.

## Run

```sh
uv run python -m agent_fs_bench run agent-replay --dirs /mnt/fs-a /mnt/fs-b
```

`CONFIG` in `agent_fs_bench/benchmarks/agent_replay.py` sets
`cases_file = local/agent-replay.toml` and `repeats = 5`. Each target runs all
cases in order; each case runs its repeats in order. Before every repeat, GNU
`cp -a --reflink=never` restores all inputs into a fresh
`<target>/agent-fs-bench/agent-replay/<run-id>/<case>/repeat-N/mount-I`. The
copy is fingerprinted and compared with the source fingerprint
(`initial_sha256`). Any mismatch in content, mode, symlinks or hard links stops
the run before replay. Fingerprints ignore timestamps, owner, xattrs and ACLs.

Restore and fingerprinting are untimed and warm the cache, so results are not
cold-cache.

## Tables and figures

`results/agent-replay/<run-id>/` holds one `manifest.json` plus flat CSV tables:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `replays` | `case`, `repeat` | `elapsed_s`, `model_requests` |
| `phases` | `case`, `repeat`, `phase` | `seconds` |

A `replays` row is one `target × case × repeat`; a `phases` row is one phase
Harbor reported for that replay. Because both tables have a `repeat`
column, `summary/<table>.csv` holds the median of each metric per target and the
remaining keys, with `n` samples. `figures/` holds `replay-time.pdf` (box plot of
`elapsed_s` per case) and one `replay-phases-<case>.pdf` per case (box plot of
`seconds` per Harbor phase; punctuation in the case name becomes `_`).
`python -m agent_fs_bench plot <run-dir>...` re-renders them.

Per-case facts — model name, and the SHA-256 of the task, cassette and each
initial mount — are in `manifest.json` under `targets[i].info`, together with the
Python/Harbor/mitmproxy versions and the submodule source hash. Response bodies
are never written to the tables. Raw upstream diagnostics go to
`results/agent-replay/<run-id>/<target>/<case>/repeat-N/`.

## Metrics

| Metric | Boundary |
| --- | --- |
| `elapsed_s` (`replays`) | Upstream `elapsed_sec`: Harbor subprocess start to exit, including environment setup/cleanup, agent, verifier, artifacts and Harbor's own output |
| `seconds` (`phases`) | Phase time reported by Harbor in `phase_seconds`; a phase Harbor does not report has no row, it is not zero |
| `model_requests` (`replays`) | Model requests received during this replay; diagnostic only, not proof of trace equivalence |

`elapsed_s` excludes input restore, fingerprinting, proxy start/stop, runner
post-processing and CSV writes. The verifier does real I/O and is timed even
when reward is zero. Model calls have no inference latency, but fixed waits in the
recorded commands remain. Terminal output, shell branches, background jobs and
slow filesystems can change real I/O, so do not claim identical syscalls or task
correctness on every target.

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
directory as `/app` initial data, and run `run agent-replay`. This toy task is
not a paper workload.
