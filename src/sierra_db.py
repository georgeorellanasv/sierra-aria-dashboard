"""
SQLite schema + helpers for the Sierra scraper.

Tables (in dependency order):
  sessions              — one row per conversation (from conversationListQuery)
  session_tags          — (session_id, tag) — many-to-many
  session_journeys      — (session_id, journey_id) — which journeys fired
  session_details       — extra per-session fields from useLogQuery
  messages              — transcript turns (from useLogQuery)
  traces                — LLM + tool-call trace events (from onDemandRedactedTraces)
  monitor_results       — monitor hits per session
  journeys              — one row per journey (from JOURNEYS_LIST + JOURNEY_DETAIL)
  journey_blocks        — parsed top-level blocks inside a journey's Lexical state
  tools                 — agent tools (from TOOLS_LIST)
  kb_sources            — knowledge source registry
  kb_articles           — articles within a KB source
  classifications       — Sonnet-generated labels + suggestions per session
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src import config


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id                        TEXT PRIMARY KEY,   -- audit-*
    timestamp_epoch           INTEGER NOT NULL,
    timestamp_iso             TEXT    NOT NULL,
    duration_seconds          INTEGER,
    device                    TEXT,
    release_target            TEXT,
    review_status             TEXT,
    first_user_message        TEXT,
    message_count             INTEGER,
    message_count_with_greeting INTEGER,
    mcp_turn_count            INTEGER,
    mcp_conversation_title    TEXT,
    raw_list_json             TEXT,
    scraped_at                TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(timestamp_epoch);

CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (session_id, tag),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);

CREATE TABLE IF NOT EXISTS session_journeys (
    session_id TEXT NOT NULL,
    journey_id TEXT NOT NULL,
    PRIMARY KEY (session_id, journey_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_details (
    id                         TEXT PRIMARY KEY,
    external_conversation_key  TEXT,
    external_chat_system       TEXT,
    external_conversation_id   TEXT,
    external_conversation_url  TEXT,
    contact_center_id          TEXT,
    locale                     TEXT,
    raw_log_json               TEXT,
    fetched_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    role       TEXT,
    text       TEXT,
    timestamp  TEXT,
    raw_json   TEXT,
    PRIMARY KEY (session_id, idx),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS traces (
    session_id     TEXT NOT NULL,
    idx            INTEGER NOT NULL,
    timestamp_ms   INTEGER,
    type           TEXT,
    purpose        TEXT,        -- llmChat.purpose
    tool_name      TEXT,
    request_json   TEXT,
    response_json  TEXT,
    error          TEXT,
    raw_json       TEXT,
    PRIMARY KEY (session_id, idx),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_traces_tool ON traces(tool_name);

CREATE TABLE IF NOT EXISTS monitor_results (
    session_id TEXT NOT NULL,
    monitor_id TEXT NOT NULL,
    slug       TEXT,
    name       TEXT,
    detected   INTEGER,
    PRIMARY KEY (session_id, monitor_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS journeys (
    id                 TEXT PRIMARY KEY,
    name               TEXT,
    version_type       TEXT,         -- 'published' | 'editing'
    version_id         TEXT,
    description        TEXT,
    criteria           TEXT,
    editor_state_json  TEXT,
    markdown           TEXT,
    tools_json         TEXT,
    scraped_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS journey_blocks (
    journey_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    block_uuid TEXT,
    block_type TEXT,
    block_name TEXT,
    markdown   TEXT,
    PRIMARY KEY (journey_id, idx),
    FOREIGN KEY (journey_id) REFERENCES journeys(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_journey_blocks_name ON journey_blocks(block_name);

CREATE TABLE IF NOT EXISTS tools (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    description TEXT,
    type        TEXT,
    params_json TEXT,
    scraped_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_sources (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    type          TEXT,
    article_count INTEGER,
    origin_json   TEXT,
    scraped_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_articles (
    id            TEXT PRIMARY KEY,        -- urlHash
    source_id     TEXT NOT NULL,
    title         TEXT,
    source_url    TEXT,
    content       TEXT,
    last_updated  TEXT,
    raw_json      TEXT,
    scraped_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES kb_sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kb_articles_source ON kb_articles(source_id);

CREATE TABLE IF NOT EXISTS classifications (
    session_id               TEXT PRIMARY KEY,
    model                    TEXT NOT NULL,
    category                 TEXT,
    subcategory              TEXT,
    pain_points_json         TEXT,
    severity                 TEXT,           -- low | medium | high | critical
    suggestion               TEXT,
    related_journey_blocks   TEXT,           -- JSON array of block_names
    related_kb_articles      TEXT,           -- JSON array of article ids
    raw_response_json        TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""


def _connect(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = config.DB_PATH) -> None:
    """Create all tables if absent. Idempotent."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def connect(db_path: Path = config.DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    """INSERT ... ON CONFLICT(...) DO UPDATE. Primary key(s) must be in the row."""
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(cols)
    # Let SQLite's ON CONFLICT ... DO UPDATE handle any PK; we compute the set clause
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT DO UPDATE SET {set_clause}"
    )
    conn.execute(sql, [row[c] for c in cols])


def dumps(obj: Any) -> str:
    """JSON-serialize for a TEXT column; handles None."""
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None  # type: ignore[return-value]


if __name__ == "__main__":
    init_db()
    print(f"Initialized {config.DB_PATH}")
