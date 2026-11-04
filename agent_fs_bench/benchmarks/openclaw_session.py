#!/usr/bin/env python3
"""OpenClaw transcript SQLite write microbenchmark.

This is a compact, synthetic trace for the hot SQLite write path of an
OpenClaw per-agent transcript. It is intentionally not an OpenClaw install,
an event replay, or a JSONL benchmark.

The source snapshot used for the model is:

  https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/infra/sqlite-wal.ts
  https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/state/openclaw-agent-schema.ts
  https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/docs/reference/database-schemas/layout.md

The measured unit is one OpenClaw-like turn, committed by one writer:

  BEGIN IMMEDIATE
    4 x update session_windows.updated_at
    4 x insert transcript_events
    4 x insert transcript_event_identities
    4 x insert session_transcript_active_events
    4 x update session_transcript_index_state
    4 x insert FTS row + FTS mapping row (unless --no-fts)
    update session_windows.transcript_updated_at
  COMMIT

The payload distribution is synthetic: 90% 1-4 KiB, 9% 256 KiB, 1% 1 MiB.
Payload JSON is stored as TEXT and event_zstd remains NULL. OpenClaw uses
optional zstd for large payloads; this script deliberately avoids zstd and
therefore is an upper-bound storage workload, not a byte-for-byte replay.
The FTS text remains separate, as it does in the source hot path; the FTS5
tokenizer is simplified to the portable built-in porter tokenizer.

The WAL settings model an unmaintained single-writer connection:
wal_autocheckpoint=16384 pages. OpenClaw sets it to 0 for a worker-maintained
writer, which this intentionally small trace does not reproduce.

The shared runner places the database in an exclusive target work directory.
One portable JSONL measurement retains metrics, PRAGMAs and raw turn samples.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


from agent_fs_bench.results import RunContext
MIB = 1024 * 1024
EVENTS_PER_TURN = 4
DEFAULT_SESSIONS = 4
DEFAULT_PREFILL_EVENTS = 1000
OPENCLAW_WAL_AUTOCHECKPOINT = 16_384
OPENCLAW_JOURNAL_SIZE_LIMIT = 64 * MIB
DB_FILENAME = "session-bench.sqlite"

# Deliberate experiment settings. Edit for an ablation; values are recorded in
# every JSONL row, and private settings must never be committed.
CONFIG = {
    "duration": 30.0,
    "rate": 10.0,
    "sessions": DEFAULT_SESSIONS,
    "prefill_events": DEFAULT_PREFILL_EVENTS,
    "seed": 1,
    "no_fts": False,
    "pragma": (),
}

ALLOWED_PRAGMAS = {
    "journal_mode",
    "synchronous",
    "wal_autocheckpoint",
    "journal_size_limit",
    "cache_size",
    "mmap_size",
    "busy_timeout",
    "foreign_keys",
    "checkpoint_fullfsync",
}
NUMERIC_PRAGMAS = {
    "wal_autocheckpoint",
    "journal_size_limit",
    "cache_size",
    "mmap_size",
    "busy_timeout",
    "checkpoint_fullfsync",
}


class BenchmarkError(RuntimeError):
    pass


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenClaw transcript SQLite write microbenchmark."
    )
    parser.parse_args(argv)
    return argparse.Namespace(**CONFIG)


def parse_pragma_overrides(raw_values: Sequence[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for raw in raw_values:
        if "=" not in raw:
            raise BenchmarkError(f"invalid --pragma {raw!r}; expected NAME=VALUE")
        name, value = raw.split("=", 1)
        name = name.strip().lower()
        value = value.strip()
        if name not in ALLOWED_PRAGMAS:
            allowed = ", ".join(sorted(ALLOWED_PRAGMAS))
            raise BenchmarkError(f"unsupported PRAGMA {name!r}; choose from {allowed}")
        if not value:
            raise BenchmarkError(f"empty value for PRAGMA {name!r}")
        if name in NUMERIC_PRAGMAS:
            try:
                int(value)
            except ValueError as exc:
                raise BenchmarkError(
                    f"PRAGMA {name!r} requires an integer value"
                ) from exc
        elif name == "journal_mode":
            value = value.upper()
            if value not in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
                raise BenchmarkError(f"invalid journal_mode {value!r}")
        elif name == "synchronous":
            value = value.upper()
            if value not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
                raise BenchmarkError(f"invalid synchronous mode {value!r}")
        elif name in {"foreign_keys", "checkpoint_fullfsync"}:
            value = value.upper()
            if value not in {"ON", "OFF", "0", "1"}:
                raise BenchmarkError(f"invalid {name} value {value!r}")
        overrides[name] = value
    return overrides




def pragma_value(conn: sqlite3.Connection, name: str) -> Any:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


def collect_pragmas(conn: sqlite3.Connection) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for name in [
        "journal_mode",
        "synchronous",
        "busy_timeout",
        "foreign_keys",
        "auto_vacuum",
        "wal_autocheckpoint",
        "journal_size_limit",
        "cache_size",
        "mmap_size",
        "page_size",
        "checkpoint_fullfsync",
    ]:
        values[name] = pragma_value(conn, name)
    return values


def open_database(
    db_path: Path, overrides: Dict[str, str]
) -> sqlite3.Connection:
    fresh = not db_path.exists() or db_path.stat().st_size == 0
    conn = sqlite3.connect(
        str(db_path),
        timeout=5.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row

    # auto_vacuum must be set before the first page is allocated.
    if fresh:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")

    settings = {
        "journal_mode": "WAL",
        "synchronous": "NORMAL",
        "busy_timeout": "5000",
        "foreign_keys": "ON",
        "wal_autocheckpoint": str(OPENCLAW_WAL_AUTOCHECKPOINT),
        "journal_size_limit": str(OPENCLAW_JOURNAL_SIZE_LIMIT),
    }
    settings.update(overrides)

    conn.execute("PRAGMA busy_timeout = " + settings.pop("busy_timeout"))
    conn.execute("PRAGMA journal_mode = " + settings.pop("journal_mode"))

    # Match OpenClaw's macOS checkpoint durability setting when applicable.
    if sys.platform == "darwin" and "checkpoint_fullfsync" not in overrides:
        conn.execute("PRAGMA checkpoint_fullfsync = 1")
    for name, value in settings.items():
        conn.execute(f"PRAGMA {name} = {value}")
    return conn


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_nodes (
    session_key TEXT NOT NULL PRIMARY KEY,
    current_session_id TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_session_nodes_updated_at
    ON session_nodes(updated_at DESC, session_key);
CREATE INDEX IF NOT EXISTS idx_agent_session_nodes_current_session_id
    ON session_nodes(current_session_id);

CREATE TABLE IF NOT EXISTS session_windows (
    session_id TEXT NOT NULL PRIMARY KEY,
    session_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    transcript_updated_at INTEGER,
    status TEXT,
    FOREIGN KEY (session_key) REFERENCES session_nodes(session_key)
        ON DELETE CASCADE
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_session_windows_updated_at
    ON session_windows(updated_at DESC, session_id);
CREATE INDEX IF NOT EXISTS idx_agent_session_windows_session_key
    ON session_windows(session_key, updated_at DESC, session_id);
CREATE INDEX IF NOT EXISTS idx_agent_session_windows_created_at
    ON session_windows(created_at DESC, session_id);

CREATE TABLE IF NOT EXISTS transcript_rewrite_watermarks (
    session_id TEXT NOT NULL PRIMARY KEY,
    generation TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session_windows(session_id)
        ON DELETE CASCADE
) STRICT;

CREATE TABLE IF NOT EXISTS transcript_events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_json TEXT,
    created_at INTEGER NOT NULL,
    event_zstd BLOB,
    event_utf8_bytes INTEGER,
    navigation_json TEXT,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES session_windows(session_id)
        ON DELETE CASCADE,
    CHECK (
        (event_json IS NOT NULL AND event_zstd IS NULL)
        OR
        (
            event_json IS NULL
            AND event_zstd IS NOT NULL
            AND event_utf8_bytes BETWEEN 1 AND 4194304
            AND length(event_zstd) BETWEEN 1 AND 4194304
            AND navigation_json IS NOT NULL
        )
    )
) STRICT;

CREATE TABLE IF NOT EXISTS transcript_event_identities (
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT,
    parent_id TEXT,
    message_idempotency_key TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, event_id),
    FOREIGN KEY (session_id, seq)
        REFERENCES transcript_events(session_id, seq)
        ON DELETE CASCADE
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_transcript_message_idempotency
    ON transcript_event_identities(session_id, message_idempotency_key)
    WHERE message_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_transcript_event_identity_sequence
    ON transcript_event_identities(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_transcript_event_parent
    ON transcript_event_identities(session_id, parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_transcript_event_sequence
    ON transcript_event_identities(session_id, event_type, seq DESC);

CREATE TABLE IF NOT EXISTS session_transcript_active_events (
    session_id TEXT NOT NULL,
    active_position INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    message_position INTEGER,
    context_eligible INTEGER,
    PRIMARY KEY (session_id, active_position),
    FOREIGN KEY (session_id, event_seq)
        REFERENCES transcript_events(session_id, seq)
        ON DELETE CASCADE
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_transcript_active_event_seq
    ON session_transcript_active_events(session_id, event_seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_transcript_active_messages
    ON session_transcript_active_events(session_id, message_position)
    WHERE message_position IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_transcript_context_pending
    ON session_transcript_active_events(session_id)
    WHERE context_eligible IS NULL;

CREATE TABLE IF NOT EXISTS session_transcript_index_state (
    session_id TEXT NOT NULL PRIMARY KEY,
    indexed_seq INTEGER NOT NULL,
    leaf_event_id TEXT,
    needs_rebuild INTEGER NOT NULL DEFAULT 0,
    active_event_count INTEGER NOT NULL DEFAULT 0,
    active_message_count INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session_windows(session_id)
        ON DELETE CASCADE
) STRICT;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS session_transcript_fts USING fts5(
    text,
    session_id UNINDEXED,
    message_id UNINDEXED,
    role UNINDEXED,
    timestamp UNINDEXED,
    tokenize = 'porter'
);

CREATE TABLE IF NOT EXISTS session_transcript_fts_rows (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS idx_session_transcript_fts_rows_session_message
    ON session_transcript_fts_rows(session_id, message_id);
CREATE TRIGGER IF NOT EXISTS session_transcript_fts_rows_after_delete
AFTER DELETE ON session_transcript_fts_rows
BEGIN
    DELETE FROM session_transcript_fts WHERE rowid = OLD.id;
END;
"""


def create_schema(conn: sqlite3.Connection, use_fts: bool) -> None:
    conn.executescript(BASE_SCHEMA)
    if use_fts:
        conn.executescript(FTS_SCHEMA)


def make_text(size: int, rng: random.Random) -> str:
    """Create deterministic, tokenizer-friendly text for FTS writes."""
    words = (
        "openclaw session transcript message assistant tool result "
        "workspace context event output status running complete"
    ).split()
    start = rng.randrange(len(words))
    pieces: List[str] = []
    total = 0
    while total < size:
        word = words[(start + len(pieces)) % len(words)]
        if pieces:
            pieces.append(" ")
            total += 1
        pieces.append(word)
        total += len(word)
    return "".join(pieces)[:size]


def choose_event_size(rng: random.Random) -> int:
    point = rng.random()
    if point < 0.90:
        return rng.randint(1024, 4096)
    if point < 0.99:
        return 256 * 1024
    return MIB


def make_event(
    rng: random.Random,
    session_id: str,
    seq: int,
    parent_id: Optional[str],
    created_at: int,
    target_size: int,
    role: str,
) -> Tuple[str, str, str]:
    event_id = f"{session_id}-event-{seq}"
    body = make_text(max(1, target_size - 192), rng)
    message: Dict[str, Any] = {
        "role": role,
        "content": [{"type": "text", "text": body}],
    }
    if role == "toolResult":
        message.update(
            {
                "toolCallId": f"tool-{seq}",
                "toolName": "exec",
                "isError": False,
            }
        )
    event_json = json.dumps(
        {
            "type": "message",
            "id": event_id,
            "parentId": parent_id,
            "timestamp": str(created_at),
            "message": message,
        },
        separators=(",", ":"),
    )
    return event_id, body, event_json


def seed_sessions(
    conn: sqlite3.Connection,
    rng: random.Random,
    session_count: int,
    prefill_events: int,
    use_fts: bool,
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, Optional[str]], int]:
    now_ms = int(time.time() * 1000)
    next_seq: Dict[str, int] = {}
    next_message_position: Dict[str, int] = {}
    leaf_ids: Dict[str, Optional[str]] = {}
    next_fts_rowid = 1

    conn.execute("BEGIN IMMEDIATE")
    try:
        for session_number in range(1, session_count + 1):
            session_key = f"session-{session_number}"
            session_id = f"{session_key}-generation-1"
            entry_json = json.dumps(
                {"sessionId": session_id, "status": "done"},
                separators=(",", ":"),
            )
            conn.execute(
                """
                INSERT INTO session_nodes (
                    session_key, current_session_id, entry_json, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (session_key, session_id, entry_json, now_ms),
            )
            conn.execute(
                """
                INSERT INTO session_windows (
                    session_id, session_key, created_at, updated_at,
                    transcript_updated_at, status
                ) VALUES (?, ?, ?, ?, ?, 'done')
                """,
                (session_id, session_key, now_ms, now_ms, now_ms),
            )
            conn.execute(
                """
                INSERT INTO transcript_rewrite_watermarks (
                    session_id, generation, updated_at
                ) VALUES (?, ?, ?)
                """,
                (session_id, f"{session_number:032x}", now_ms),
            )

            events: List[Tuple[Any, ...]] = []
            identities: List[Tuple[Any, ...]] = []
            active_events: List[Tuple[Any, ...]] = []
            fts_rows: List[Tuple[Any, ...]] = []
            fts_entries: List[Tuple[Any, ...]] = []
            leaf_event_id: Optional[str] = None

            for seq in range(prefill_events):
                created_at = now_ms + seq
                role = "assistant"
                event_id, body, event_json = make_event(
                    rng,
                    session_id,
                    seq,
                    leaf_event_id,
                    created_at,
                    rng.randint(128, 384),
                    role,
                )
                encoded_size = len(event_json.encode("utf-8"))
                events.append(
                    (
                        session_id,
                        seq,
                        event_json,
                        created_at,
                        None,
                        encoded_size,
                        None,
                    )
                )
                identities.append(
                    (
                        session_id,
                        event_id,
                        seq,
                        "message",
                        leaf_event_id,
                        f"{session_id}:prefill-idem:{seq}",
                        created_at,
                    )
                )
                active_events.append((session_id, seq, seq, seq, 1))
                if use_fts:
                    fts_rows.append((next_fts_rowid, session_id, event_id))
                    fts_entries.append(
                        (
                            next_fts_rowid,
                            body,
                            session_id,
                            event_id,
                            role,
                            created_at,
                        )
                    )
                    next_fts_rowid += 1
                leaf_event_id = event_id

            if events:
                conn.executemany(
                    """
                    INSERT INTO transcript_events (
                        session_id, seq, event_json, created_at, event_zstd,
                        event_utf8_bytes, navigation_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    events,
                )
                conn.executemany(
                    """
                    INSERT INTO transcript_event_identities (
                        session_id, event_id, seq, event_type, parent_id,
                        message_idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    identities,
                )
                conn.executemany(
                    """
                    INSERT INTO session_transcript_active_events (
                        session_id, active_position, event_seq,
                        message_position, context_eligible
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    active_events,
                )
                if use_fts:
                    conn.executemany(
                        """
                        INSERT INTO session_transcript_fts_rows (
                            id, session_id, message_id
                        ) VALUES (?, ?, ?)
                        """,
                        fts_rows,
                    )
                    conn.executemany(
                        """
                        INSERT INTO session_transcript_fts (
                            rowid, text, session_id, message_id, role, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        fts_entries,
                    )

            conn.execute(
                """
                INSERT INTO session_transcript_index_state (
                    session_id, indexed_seq, leaf_event_id, needs_rebuild,
                    active_event_count, active_message_count, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    session_id,
                    prefill_events - 1,
                    leaf_event_id,
                    prefill_events,
                    prefill_events,
                    now_ms + max(0, prefill_events - 1),
                ),
            )
            next_seq[session_key] = prefill_events
            next_message_position[session_key] = prefill_events
            leaf_ids[session_key] = leaf_event_id

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return next_seq, next_message_position, leaf_ids, next_fts_rowid


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def latency_summary(samples: Sequence[float]) -> Dict[str, Optional[float]]:
    if not samples:
        return {
            "count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    ordered = sorted(samples)

    def percentile(percent: int) -> float:
        index = max(0, math.ceil(percent / 100.0 * len(ordered)) - 1)
        return ordered[index]

    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "max": ordered[-1],
    }


def write_turn(
    conn: sqlite3.Connection,
    rng: random.Random,
    use_fts: bool,
    session_id: str,
    seq_start: int,
    message_position_start: int,
    parent_id: Optional[str],
    fts_rowid_start: int,
) -> Tuple[int, int, str, int]:
    """Construct a turn payload, then execute its write transaction."""
    roles = ("user", "assistant", "toolResult", "assistant")
    now_ms = int(time.time() * 1000)
    previous_event_id = parent_id
    events: List[Tuple[Any, ...]] = []
    identities: List[Tuple[Any, ...]] = []
    active_events: List[Tuple[Any, ...]] = []
    fts_rows: List[Tuple[Any, ...]] = []
    fts_entries: List[Tuple[Any, ...]] = []
    payload_bytes = 0
    message_position = message_position_start
    next_fts_rowid = fts_rowid_start

    for offset, role in enumerate(roles):
        seq = seq_start + offset
        created_at = now_ms + offset
        event_id, body, event_json = make_event(
            rng,
            session_id,
            seq,
            previous_event_id,
            created_at,
            choose_event_size(rng),
            role,
        )
        encoded_size = len(event_json.encode("utf-8"))
        payload_bytes += encoded_size
        events.append(
            (session_id, seq, event_json, created_at, None, encoded_size, None)
        )
        idempotency_key = (
            None if role == "toolResult" else f"{session_id}:idem:{seq}"
        )
        identities.append(
            (
                session_id,
                event_id,
                seq,
                "message",
                previous_event_id,
                idempotency_key,
                created_at,
            )
        )
        active_events.append((session_id, seq, seq, message_position, 1))
        message_position += 1
        if use_fts:
            fts_rows.append((next_fts_rowid, session_id, event_id))
            fts_entries.append(
                (
                    next_fts_rowid,
                    body,
                    session_id,
                    event_id,
                    role,
                    created_at,
                )
            )
            next_fts_rowid += 1
        previous_event_id = event_id

    conn.execute("BEGIN IMMEDIATE")
    try:
        # These read checks mirror the insert path's identity and watermark
        # lookups, so the benchmark is not a write-only idealization.
        conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM transcript_events "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.execute(
            "SELECT active_event_count, active_message_count, indexed_seq, "
            "leaf_event_id, needs_rebuild FROM session_transcript_index_state "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        for row in identities:
            event_id = row[1]
            conn.execute(
                "SELECT 1 FROM transcript_event_identities "
                "WHERE session_id = ? AND event_id = ? LIMIT 1",
                (session_id, event_id),
            ).fetchone()
            idempotency_key = row[5]
            if idempotency_key is not None:
                conn.execute(
                    "SELECT event_id FROM transcript_event_identities "
                    "WHERE session_id = ? AND message_idempotency_key = ? LIMIT 1",
                    (session_id, idempotency_key),
                ).fetchone()

        for offset in range(EVENTS_PER_TURN):
            conn.execute(
                "UPDATE session_windows SET updated_at = ? WHERE session_id = ?",
                (now_ms + offset, session_id),
            )

        conn.executemany(
            """
            INSERT INTO transcript_events (
                session_id, seq, event_json, created_at, event_zstd,
                event_utf8_bytes, navigation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            events,
        )
        conn.executemany(
            """
            INSERT INTO transcript_event_identities (
                session_id, event_id, seq, event_type, parent_id,
                message_idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            identities,
        )
        conn.executemany(
            """
            INSERT INTO session_transcript_active_events (
                session_id, active_position, event_seq,
                message_position, context_eligible
            ) VALUES (?, ?, ?, ?, ?)
            """,
            active_events,
        )

        if use_fts:
            conn.executemany(
                """
                INSERT INTO session_transcript_fts_rows (
                    id, session_id, message_id
                ) VALUES (?, ?, ?)
                """,
                fts_rows,
            )
            conn.executemany(
                """
                INSERT INTO session_transcript_fts (
                    rowid, text, session_id, message_id, role, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                fts_entries,
            )

        for offset, event_row in enumerate(events):
            seq = event_row[1]
            conn.execute(
                """
                UPDATE session_transcript_index_state
                SET indexed_seq = ?, leaf_event_id = ?, needs_rebuild = 0,
                    active_event_count = ?, active_message_count = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    seq,
                    identities[offset][1],
                    seq + 1,
                    message_position_start + offset + 1,
                    now_ms + offset,
                    session_id,
                ),
            )

        conn.execute(
            """
            UPDATE session_windows
            SET transcript_updated_at = ?
            WHERE session_id = ?
            """,
            (now_ms + EVENTS_PER_TURN - 1, session_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return payload_bytes, message_position, previous_event_id, next_fts_rowid


def measure(args: argparse.Namespace, overrides: Dict[str, str]) -> Dict[str, Any]:
    use_fts = not args.no_fts
    conn = open_database(args.db, overrides)
    result: Dict[str, Any] = {
        "workload": "openclaw-transcript-write",
        "source_snapshot": "openclaw@5f6f15fed0d6ba618cf781c9686a8b93f499564b",
        "db_path": str(args.db),
        "sqlite_version": sqlite3.sqlite_version,
        "fts_projection": use_fts,
        "seed": args.seed,
        "sessions": args.sessions,
        "prefill_events_per_session": args.prefill_events,
        "events_per_turn": EVENTS_PER_TURN,
        "duration_target_s": args.duration,
        "rate_target_turns_per_s": args.rate,
        "pragma_overrides": overrides,
        "pragma_scope": (
            "foreign_keys、busy_timeout、wal_autocheckpoint 和 "
            "checkpoint_fullfsync 是连接级参数；pragmas 中的值在 benchmark "
            "连接关闭前读取。"
        ),
    }
    try:
        create_schema(conn, use_fts)
        (
            next_seq,
            message_positions,
            leaf_ids,
            next_fts_rowid,
        ) = seed_sessions(
            conn,
            random.Random(args.seed),
            args.sessions,
            args.prefill_events,
            use_fts,
        )

        # The seed is setup, not measured. Reset the WAL before the trace so
        # the result starts from a clean, comparable sidecar state.
        if str(pragma_value(conn, "journal_mode")).lower() == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        db_path = args.db
        wal_path = Path(str(db_path) + "-wal")
        shm_path = Path(str(db_path) + "-shm")
        start_db = file_size(db_path)
        start_wal = file_size(wal_path)
        db_peak = start_db
        wal_peak = start_wal
        transaction_ms: List[float] = []
        payload_bytes = 0
        turns = 0
        rng = random.Random(args.seed ^ 0x5F3759DF)
        session_keys = [
            f"session-{index}" for index in range(1, args.sessions + 1)
        ]

        started = time.perf_counter()
        cpu_started = time.process_time()
        deadline = started + args.duration
        while True:
            if args.rate > 0:
                scheduled = started + turns / args.rate
                delay = scheduled - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            if time.perf_counter() >= deadline:
                break

            session_key = session_keys[rng.randrange(len(session_keys))]
            session_id = f"{session_key}-generation-1"
            tx_started = time.perf_counter_ns()
            (
                turn_payload_bytes,
                message_positions[session_key],
                leaf_ids[session_key],
                next_fts_rowid,
            ) = write_turn(
                conn,
                rng,
                use_fts,
                session_id,
                next_seq[session_key],
                message_positions[session_key],
                leaf_ids[session_key],
                next_fts_rowid,
            )
            transaction_ms.append(
                (time.perf_counter_ns() - tx_started) / 1_000_000.0
            )
            next_seq[session_key] += EVENTS_PER_TURN
            turns += 1
            payload_bytes += turn_payload_bytes

            # File-size sampling is intentionally outside the commit timer.
            db_peak = max(db_peak, file_size(db_path))
            wal_peak = max(wal_peak, file_size(wal_path))

        elapsed = time.perf_counter() - started
        cpu_elapsed = time.process_time() - cpu_started
        end_db = file_size(db_path)
        end_wal = file_size(wal_path)
        end_shm = file_size(shm_path)

        checkpoint_started = time.perf_counter()
        if str(pragma_value(conn, "journal_mode")).lower() == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000.0
        after_checkpoint_db = file_size(db_path)
        after_checkpoint_wal = file_size(wal_path)

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        event_rows = conn.execute(
            "SELECT COUNT(*) FROM transcript_events"
        ).fetchone()[0]
        identity_rows = conn.execute(
            "SELECT COUNT(*) FROM transcript_event_identities"
        ).fetchone()[0]
        active_rows = conn.execute(
            "SELECT COUNT(*) FROM session_transcript_active_events"
        ).fetchone()[0]
        fts_rows: Optional[int] = None
        if use_fts:
            fts_rows = conn.execute(
                "SELECT COUNT(*) FROM session_transcript_fts_rows"
            ).fetchone()[0]

        events = turns * EVENTS_PER_TURN
        growth = max(0, end_db + end_wal - start_db - start_wal)
        result.update(
            {
                "generated_at_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "pragmas": collect_pragmas(conn),
                "duration_actual_s": elapsed,
                "turns": turns,
                "new_events": events,
                "payload_bytes": payload_bytes,
                "turns_per_s": turns / elapsed if elapsed else 0.0,
                "events_per_s": events / elapsed if elapsed else 0.0,
                "payload_mib_per_s": (
                    payload_bytes / MIB / elapsed if elapsed else 0.0
                ),
                "process_cpu_s": cpu_elapsed,
                "latency_ms": {
                    "turn_transaction": latency_summary(transaction_ms)
                },
                "turn_samples_ms": transaction_ms,
                "files": {
                    "db_bytes_before": start_db,
                    "wal_bytes_before": start_wal,
                    "db_bytes_after": end_db,
                    "wal_bytes_after": end_wal,
                    "shm_bytes_after": end_shm,
                    "db_peak_bytes": db_peak,
                    "wal_peak_bytes": wal_peak,
                    "db_and_wal_growth_per_payload_byte": (
                        growth / payload_bytes if payload_bytes else 0.0
                    ),
                },
                "final_checkpoint": {
                    "elapsed_ms": checkpoint_ms,
                    "db_bytes_after": after_checkpoint_db,
                    "wal_bytes_after": after_checkpoint_wal,
                },
                "rows": {
                    "transcript_events": event_rows,
                    "transcript_event_identities": identity_rows,
                    "session_transcript_active_events": active_rows,
                    "session_transcript_fts_rows": fts_rows,
                },
                "integrity_check": integrity,
                "storage_notes": (
                    "event_json is TEXT; zstd is intentionally disabled. "
                    "FTS is included by default because OpenClaw's transcript "
                    "index appender writes it in the same transaction."
                ),
            }
        )
        if integrity != "ok":
            raise BenchmarkError(f"integrity_check={integrity}")
        return result
    finally:
        conn.close()


def run(ctx: RunContext, args: argparse.Namespace) -> None:
    args.db = ctx.work_dir / DB_FILENAME
    overrides = parse_pragma_overrides(args.pragma)
    result = measure(args, overrides)
    ctx.parameters["sqlite_version"] = sqlite3.sqlite_version
    ctx.parameters["effective_pragmas"] = result["pragmas"]
    metrics = {name: result[name] for name in (
        "duration_actual_s", "turns", "new_events", "payload_bytes",
        "turns_per_s", "events_per_s", "payload_mib_per_s", "process_cpu_s",
    )}
    metrics.update({f"turn_{name}_ms": value for name, value in
                    result["latency_ms"]["turn_transaction"].items()
                    if name != "count" and value is not None})
    metrics["checkpoint_ms"] = result["final_checkpoint"]["elapsed_ms"]
    result["fsync_mode"] = f"SQLite synchronous={result['pragmas']['synchronous']}"
    result["cache_state"] = "warm: seeded database; no cache drop"
    result["latency_scope"] = (
        "write_turn call including payload construction and transaction; "
        "excludes pacing, file sampling, and final checkpoint"
    )
    ctx.emit("transcript-write", metrics, details=result)
