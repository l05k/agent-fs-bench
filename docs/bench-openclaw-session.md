# OpenClaw session SQLite benchmark

A single-writer SQLite microbenchmark that mimics the hot write path of an
OpenClaw per-agent transcript: one transaction per simulated turn.

单写者 SQLite 微基准，模拟 OpenClaw 单智能体 transcript 的热写路径：
每个模拟 turn 一个事务。

## Artifacts / 产物

| Path | Where | Purpose |
| --- | --- | --- |
| `agent_fs_bench/benchmarks/openclaw_session.py` | shared package | workload (stdlib + SQLite) |
| `<target>/agent-fs-bench/openclaw-session/<run-id>/session-bench.sqlite` | target FS | fresh database + WAL/SHM |
| `results/openclaw-session/<run-id>/results.jsonl` | project | one portable measurement per target |
| `results/openclaw-session/<run-id>/summary.csv` | project | derived target/case/metric statistics |

Each run seeds a fresh database in its exclusive work directory. The shared
runner derives `summary.csv` only after all selected targets succeed. If a
later target fails, earlier successful measurements remain in JSONL without
an automatic CSV. No duplicate JSON document, Markdown report, template or
manifest is generated.

## Run / 运行

```sh
uv run python -m agent_fs_bench run openclaw-session --dirs /mnt/fs-a
```

There are no workload flags. Duration, rate, sessions, prefill, seed, FTS
projection and PRAGMA overrides are the deliberate `CONFIG` mapping at the top
of `agent_fs_bench/benchmarks/openclaw_session.py`. JSONL records the workload
inputs and the effective PRAGMAs, without repeating the override strings.
Defaults: 30 s, 10 turns/s, 4 sessions, 1000 prefill events/session, FTS on,
seed 1, no PRAGMA overrides.

## Schema and workload / 模式与负载

Tables model OpenClaw's transcript store (`session_nodes`, `session_windows`,
`transcript_events`, `transcript_event_identities`,
`session_transcript_active_events`, `session_transcript_index_state`,
`session_transcript_fts` + `session_transcript_fts_rows`, all `STRICT`).

**Setup (untimed)** — create schema, seed `CONFIG["prefill_events"]` events per
session, then `wal_checkpoint(TRUNCATE)` so every run starts from the same
sidecar state.

**Timed loop** — pick a session at random and run one `write_turn`:

```text
BEGIN IMMEDIATE
  4 × SELECT (seq watermark, index state, identity probes)
  4 × UPDATE session_windows.updated_at
  4 × INSERT transcript_events
  4 × INSERT transcript_event_identities
  4 × INSERT session_transcript_active_events
  4 × UPDATE session_transcript_index_state
  4 × INSERT FTS row + FTS mapping row        (unless CONFIG["no_fts"])
  UPDATE session_windows.transcript_updated_at
COMMIT
```

Payloads: 90% 1–4 KiB, 9% 256 KiB, 1% 1 MiB, stored as `event_json` TEXT with
`event_zstd` NULL (an upper bound, not a byte-for-byte replay).

**Afterwards** — final `wal_checkpoint(TRUNCATE)` (timed separately),
`PRAGMA integrity_check`, row counts, and file-size sampling.

## Output / 输出

`results.jsonl` records throughput, latency statistics and checkpoint time only
in `metrics`. `parameters` holds workload inputs, the SQLite version and PRAGMAs
read from the measured connection. `details` retains raw `turn_samples_ms`, row
counts, DB/WAL sizes and peaks, post-checkpoint sizes and the integrity check.
Versions, settings and numeric metrics are not duplicated in `details`.
The common summary/plot merges runs globally; the operator ensures comparable
conditions, and CSV `run_ids` identifies the contributing runs.

The existing latency timer surrounds the entire `write_turn` call, including
payload construction and the transaction. Pacing, file-size sampling and the
final checkpoint are outside this timer. It is not pure BEGIN-to-COMMIT latency.

Exit code is 0 only when `integrity_check == "ok"`.

## Notes / 注意事项

- Sampled file sizes (`db_peak_bytes`, `wal_peak_bytes`) are read between
  transactions and are lower bounds. 文件大小采样是下界。
- `wal_autocheckpoint` is pinned to 16384 pages (64 MiB) and
  `journal_size_limit` to 64 MiB to model an unmaintained single writer;
  OpenClaw itself uses a worker-maintained checkpointer.
- PRAGMA overrides come from `CONFIG["pragma"]` (`NAME=VALUE` strings in code)
  and are validated; their actual connection values are recorded in JSONL.
- To disable FTS for an ablation, set `CONFIG["no_fts"] = True`; the default keeps
  the synchronous FTS projection because OpenClaw writes it in the same
  transaction.
- Synthetic payloads, one process, one writer: no zstd, cold archive,
  concurrent writers or real session replay. 合成负载，单进程单写入者。
