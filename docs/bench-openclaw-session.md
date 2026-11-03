# OpenClaw session SQLite benchmark

A single-writer SQLite microbenchmark that mimics the hot write path of an
OpenClaw per-agent transcript: one transaction per simulated turn.

单写者 SQLite 微基准，模拟 OpenClaw 单智能体 transcript 的热写路径：
每个模拟 turn 一个事务。

## Artifacts / 产物

| Path | Where | Purpose |
| --- | --- | --- |
| `openclaw_session_bench.py` | repo root | the benchmark (stdlib + SQLite only) |
| `<--dir>/openclaw-session-bench/session-bench.sqlite` | target FS | database + WAL/SHM |
| `results/openclaw_session_bench/<UTC-run-id>/` | project | reports |

Report files: `result.json`, `report.md`, `report.md.tmpl`, `summary.txt`,
`manifest.txt`. The database is deleted at the start of every run, so each run
re-seeds from scratch.

## Run / 运行

```sh
uv run openclaw_session_bench.py --dir /mnt/testfs --duration 30
uv run openclaw_session_bench.py --dir /mnt/testfs --rate 0 --sessions 8
uv run openclaw_session_bench.py --dir /mnt/testfs --pragma synchronous=FULL
uv run openclaw_session_bench.py --dir /mnt/testfs --no-fts      # ablation only
```

Defaults: `--duration 30`, `--rate 10` turns/s, `--sessions 4`,
`--prefill-events 1000`, FTS on, `--seed 1`.

## Schema and workload / 模式与负载

Tables model OpenClaw's transcript store (`session_nodes`, `session_windows`,
`transcript_events`, `transcript_event_identities`,
`session_transcript_active_events`, `session_transcript_index_state`,
`session_transcript_fts` + `session_transcript_fts_rows`, all `STRICT`).

**Setup (untimed)** — create schema, seed `--prefill-events` events per session,
then `wal_checkpoint(TRUNCATE)` so every run starts from the same sidecar state.

**Timed loop** — pick a session at random and run one `write_turn`:

```text
BEGIN IMMEDIATE
  4 × SELECT (seq watermark, index state, identity probes)
  4 × UPDATE session_windows.updated_at
  4 × INSERT transcript_events
  4 × INSERT transcript_event_identities
  4 × INSERT session_transcript_active_events
  4 × UPDATE session_transcript_index_state
  4 × INSERT FTS row + FTS mapping row        (unless --no-fts)
  UPDATE session_windows.transcript_updated_at
COMMIT
```

Payloads: 90% 1–4 KiB, 9% 256 KiB, 1% 1 MiB, stored as `event_json` TEXT with
`event_zstd` NULL (an upper bound, not a byte-for-byte replay).

**Afterwards** — final `wal_checkpoint(TRUNCATE)` (timed separately),
`PRAGMA integrity_check`, row counts, and file-size sampling.

## Output / 输出

`result.json` reports environment (Python/SQLite/platform), pragmas actually in
effect, `turns`, `new_events`, `turns_per_s`, `events_per_s`,
`payload_mib_per_s`, `process_cpu_s`, per-turn transaction latency
(count/mean/p50/p95/p99/max), DB/WAL sizes and peaks, storage amplification,
final checkpoint time, row counts and `integrity_check`.

Exit code is 0 only when `integrity_check == "ok"`.

## Notes / 注意事项

- Sampled file sizes (`db_peak_bytes`, `wal_peak_bytes`) are read between
  transactions and are lower bounds. 文件大小采样是下界。
- `wal_autocheckpoint` is pinned to 16384 pages (64 MiB) and
  `journal_size_limit` to 64 MiB to model an unmaintained single writer;
  OpenClaw itself uses a worker-maintained checkpointer.
- `--pragma` accepts only the whitelisted connection pragmas; values are
  validated and recorded in `result.json`.
- `--no-fts` is an ablation; the default keeps the synchronous FTS projection
  because OpenClaw writes it in the same transaction.
- Synthetic payloads, one process, one writer: no zstd, cold archive,
  concurrent writers or real session replay. 合成负载，单进程单写入者。
