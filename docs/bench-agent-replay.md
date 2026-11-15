# Agent replay

Runs the selected Harbor task cohort on each filesystem using separate Codex
and OpenClaw model-response cassettes. Only the declared task-data mounts use
the target. The verifier runs, but correctness of the solution (reward) does
not select timing samples. Command, output or response divergence does.

## Prepare

- Linux and local Docker/Compose on each participating machine; use the pinned
  `third_party/agent-mitm-replay` submodule and its locked Python 3.13 environment.
- Pin task releases, images and harnesses. Before capture, freeze task IDs and
  the selection seed: 100 uniform distinct tasks from SWE-bench Pro V2 public,
  plus all 89 Terminal-Bench 2.0 tasks for the paper. Use the same IDs under
  both harnesses. Archive this manifest before collecting data.
- Put the task directories, initial snapshots and cassettes on local control
  storage. Copy `experiments/agent-replay.example.toml` to
  `local/agent-replay.toml`; list every planned task/harness, including any whose
  cassette ultimately cannot be captured. Each entry gives its benchmark, task
  ID, agent, task, cassette, and one or more distinct container mounts.
- Record each pair independently on ext4 from a disposable copy of its initial
  data; use the same Qwen deployment via the corresponding Responses and Chat
  Completions wire protocols. The provider key is read only at capture.

```sh
git submodule update --init --recursive
uv sync --locked --project third_party/agent-mitm-replay
```

See the [submodule usage](../third_party/agent-mitm-replay/docs/usage.md) for
native versions, capture and Docker smoke commands. Archive the task manifests,
image digests and redacted experiment configuration. Cassettes remain private.

## Measure and summarize

```sh
uv run python -m agent_fs_bench run agent-replay --target ext4
uv run python -m agent_fs_bench run agent-replay --target apsara
uv run python -m agent_fs_bench.replay_report \
  results/agent-replay/<ext4-run> results/agent-replay/<apsara-run> --reference ext4
```

`CONFIG` selects the manifest (`cases_file`), concurrency (`tasks`, default 1),
repeat count (`repeats`, default 5), and optional single-case development filter
(`case`, default `None` for the full manifest). For the paper use `tasks=1`,
same case manifest and repeat count on every backend, and interleave the target
order across runs. Pin host resources and filesystem options. Run on ordinary
POSIX modes; the replay does not inject agent-step synchronization.

Each rank restores and verifies the pristine content, mode and links of its
private data and completes a target-scoped `syncfs` before the shared start
barrier. Restoration warms caches and is not timed. Classified unsuccessful
replays retain logs without a latency. Unexpected setup errors fail the run.

| Table | Meaning |
| --- | --- |
| `outcomes` | One planned observation per target/case/repeat/rank, including `capture_missing`, `divergence`, `error` and `timeout` |
| `replays` | Valid observations only: `tool_s`, diagnostic `harbor_s` and `model_requests` |
| `phases` | Harbor phase durations for accepted observations, not summed into `tool_s` |

The paired report checks per-target completion, identical cohort identities and
one row per planned observation. It reports outcome counts, planned and valid
tasks per benchmark/harness, and the distribution of task-level ratios. A task
enters the paired sample only when every backend has all prescribed valid
repeats and its ext4 median tool time is positive. The denominator is that
same task's ext4 median; take the median over these ratios, never the ratio of
medians across tasks. Default `plot` figures are diagnostic single-case tool
time distributions, not the paper's paired plot.
