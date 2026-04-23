"""
Scrape Sierra conversations into data/sierra.db.

Fetches:
  1. The list of sessions via conversationListQuery (+ pagination)
  2. For each session:
     - useLogQuery         -> transcript (entries), tags, journeyIds, device, locale
     - onDemandRedactedTraces -> LLM + tool-call traces
     - useLogSystemMonitorResultsQuery -> monitor hits

Filters the time range. Defaults to "last 24 hours" but can be overridden with
--since / --until (epoch seconds) or SCRAPE_DATE_FROM / SCRAPE_DATE_TO in .env
as YYYY-MM-DD (interpreted as calendar days in local time).

Usage:
  python scripts/scrape_sessions.py               # last 24h
  python scripts/scrape_sessions.py --today       # midnight local -> now
  python scripts/scrape_sessions.py --since 2026-04-22 --until 2026-04-22
  python scripts/scrape_sessions.py --max 10      # cap number of sessions (smoke)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, queries, sierra_db
from src.sierra_client import SierraClient

logger = logging.getLogger("scrape_sessions")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)


def parse_date(s: str) -> int:
    """Parse YYYY-MM-DD as midnight local time -> epoch seconds."""
    dt = datetime.strptime(s, "%Y-%m-%d").astimezone()
    return int(dt.timestamp())


def build_time_range(args: argparse.Namespace) -> dict | None:
    if args.since or args.until:
        start = parse_date(args.since) if args.since else 0
        end = parse_date(args.until) + 86400 if args.until else int(time.time())
        return {"startSeconds": start, "endSeconds": end}
    if args.today:
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return {"startSeconds": int(midnight.timestamp()), "endSeconds": int(time.time())}
    # Default: last 24h
    now = int(time.time())
    return {"startSeconds": now - 86400, "endSeconds": now}


def iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat()


# ---------------------------------------------------------------------------
# Session list + pagination
# ---------------------------------------------------------------------------

def fetch_session_list(
    client: SierraClient,
    time_range: dict | None,
    max_sessions: int | None,
) -> list[dict]:
    base_vars = {
        "agentId":              client.agent_id,
        "search":               None,
        "timeRange":            time_range,
        "tags":                 [],
        "journeys":             [],
        "reviewStatuses":       [],
        "releases":             [],
        "targets":              [],
        "channels":             [],
        "containsUserMessages": False,
        "filterQuery":          None,
    }

    # First page via CONVERSATION_LIST.
    data = client.query(queries.CONVERSATION_LIST, base_vars,
                        operation_label="conversationListQuery")
    node = data.get("node") or {}
    logs_conn = node.get("logs") or {}
    edges = logs_conn.get("edges") or []
    page_info = logs_conn.get("pageInfo") or {}
    sessions = [e.get("node") for e in edges if e.get("node")]
    logger.info("Page 1: got %d sessions", len(sessions))

    # Follow pagination via CONVERSATION_LIST_PAGINATION.
    while page_info.get("hasNextPage") and (not max_sessions or len(sessions) < max_sessions):
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        page_vars = {
            **base_vars,
            "count":  50,
            "cursor": cursor,
            "id":     client.agent_id,
        }
        page_data = client.query(queries.CONVERSATION_LIST_PAGINATION, page_vars,
                                 operation_label="conversationListPaginationQuery")
        page_node = page_data.get("node") or {}
        logs_conn = page_node.get("logs") or {}
        edges = logs_conn.get("edges") or []
        page_info = logs_conn.get("pageInfo") or {}
        new = [e.get("node") for e in edges if e.get("node")]
        sessions.extend(new)
        logger.info("Next page: +%d (total %d)", len(new), len(sessions))
        if not new:
            break

    if max_sessions:
        sessions = sessions[:max_sessions]
    logger.info("Session list complete: %d sessions", len(sessions))
    return sessions


# ---------------------------------------------------------------------------
# Per-session fetches
# ---------------------------------------------------------------------------

def fetch_log_detail(client: SierraClient, log_id: str) -> dict:
    v = {
        "agentId": client.agent_id,
        "logId": log_id,
        "search": None,
        "piiRevealReason": None,
        "tracePiiRevealReason": None,
        "translate": False,
        "includeMonitorResults": True,
        "includeDeveloperTags": False,
        "includeEmployeeTags": False,
    }
    data = client.query(queries.LOG_DETAIL, v, operation_label="useLogQuery")
    return ((data.get("botByID") or {}).get("logByID")) or {}


def fetch_log_traces(client: SierraClient, log_id: str) -> list[dict]:
    v = {"agentId": client.agent_id, "logId": log_id, "allowReredaction": True}
    data = client.query(queries.LOG_TRACES, v, operation_label="onDemandRedactedTracesQuery")
    log = ((data.get("botByID") or {}).get("logByID")) or {}
    return log.get("traceEvents") or []


def fetch_monitor_results(client: SierraClient, log_id: str) -> list[dict]:
    v = {"agentId": client.agent_id, "logId": log_id}
    data = client.query(queries.LOG_MONITOR_RESULTS, v,
                        operation_label="useLogSystemMonitorResultsQuery")
    log = ((data.get("botByID") or {}).get("logByID")) or {}
    return log.get("getSystemMonitorResults") or []


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _scalar(v):
    """Coerce dict/list to JSON string, keep primitives as-is."""
    if isinstance(v, (dict, list)):
        return sierra_db.dumps(v)
    return v


def store_session_row(conn, node: dict) -> None:
    row = {
        "id":                          node.get("id"),
        "timestamp_epoch":             node.get("timestamp"),
        "timestamp_iso":               iso(node.get("timestamp")),
        "duration_seconds":            node.get("duration"),
        "device":                      node.get("device"),
        "release_target":              node.get("releaseTarget"),
        "review_status":               node.get("reviewStatus"),
        "first_user_message":          _scalar(node.get("firstUserMessage")),
        "message_count":               node.get("messageCount"),
        "message_count_with_greeting": node.get("messageCountWithGreeting"),
        "mcp_turn_count":              node.get("mcpTurnCount"),
        "mcp_conversation_title":      _scalar(node.get("mcpConversationTitle")),
        "raw_list_json":               sierra_db.dumps(node),
    }
    sierra_db.upsert(conn, "sessions", row)


def store_session_detail(conn, log_id: str, log: dict) -> None:
    ext_info = log.get("externalChatInfo") or {}
    cc = log.get("contactCenterConnection") or {}
    row = {
        "id":                         log_id,
        "external_conversation_key":  log.get("externalConversationKey"),
        "external_chat_system":       ext_info.get("externalChatSystem"),
        "external_conversation_id":   ext_info.get("externalConversationId"),
        "external_conversation_url":  ext_info.get("externalConversationUrl"),
        "contact_center_id":          cc.get("id"),
        "locale":                     log.get("locale"),
        "raw_log_json":               sierra_db.dumps(log),
    }
    sierra_db.upsert(conn, "session_details", row)

    # tags
    for tag in (log.get("tags") or []):
        conn.execute(
            "INSERT OR IGNORE INTO session_tags(session_id, tag) VALUES (?, ?)",
            (log_id, tag),
        )
    # journey ids
    for jid in (log.get("journeyIds") or []):
        conn.execute(
            "INSERT OR IGNORE INTO session_journeys(session_id, journey_id) VALUES (?, ?)",
            (log_id, jid),
        )

    # messages (from entries)
    entries = log.get("entries") or []
    # Delete old, insert fresh (idempotent re-run)
    conn.execute("DELETE FROM messages WHERE session_id=?", (log_id,))
    for idx, entry in enumerate(entries):
        event = entry.get("event") or {}
        role = _infer_role(event)
        text = _extract_text(event)
        ts_ms = entry.get("timestampMs")
        conn.execute(
            "INSERT INTO messages(session_id, idx, role, text, timestamp, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                log_id,
                idx,
                role,
                text,
                iso(ts_ms // 1000) if ts_ms else None,
                sierra_db.dumps(entry),
            ),
        )


def _infer_role(event: dict | None) -> str | None:
    """Map Sierra LogEntry events to a simple role label.

    Real shapes observed:
      LogEntryMessage         -> author in USER | AGENT | SYSTEM
      LogEntryCallDisconnect  -> system
      LogEntryFileUploadConfig -> system (metadata)
      LogEntryToolCall        -> tool (if it exists)
    """
    if not event:
        return None
    typename = event.get("__typename") or ""
    if typename == "LogEntryMessage":
        author = (event.get("author") or "").lower()
        return author or "message"
    # Non-message entries: label by typename stripped of the LogEntry prefix.
    if typename.startswith("LogEntry"):
        return typename[len("LogEntry"):].lower() or "system"
    return typename.lower() or None


def _extract_text(event: dict | None) -> str | None:
    if not event:
        return None
    if event.get("text"):
        return event["text"]
    # Some events may wrap text in nested keys.
    for k in ("userMessage", "agentMessage", "systemMessage"):
        v = event.get(k)
        if isinstance(v, dict) and v.get("text"):
            return v["text"]
    return None


def _trace_fields(t: dict) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return (purpose, tool_name, request_json, response_json, error) for a trace.

    A trace event has one populated sub-object matching its `type`. We extract the
    relevant pieces per type so analytics can filter by tool name, LLM purpose, etc.
    """
    typ = t.get("type")
    purpose = tool_name = err = None
    req = resp = None

    if typ == "llmChat":
        llm = t.get("llmChat") or {}
        purpose = llm.get("purpose")
        req = llm.get("request")
    elif typ == "llmChatResponse":
        lr = t.get("llmChatResponse") or {}
        resp = lr.get("response")
        err = lr.get("responseErr")
        msg = lr.get("message") or {}
        tool_name = msg.get("name")  # function-call name, when present
    elif typ == "fetch":
        f = t.get("fetch") or {}
        tool_name = f.get("name") or f.get("url")
        req = f.get("request")
        resp = f.get("response")
        err = f.get("error")
    elif typ == "task":
        task = t.get("task") or {}
        tool_name = task.get("taskId") or task.get("id")
        req = task.get("input")
        resp = task.get("output")
    elif typ == "knowledgeSearch":
        ks = t.get("knowledgeSearch") or {}
        tool_name = "knowledgeSearch"
        req = ks.get("query") or ks
        resp = ks.get("results")
    elif typ == "mcp":
        mcp = t.get("mcp") or {}
        tool_name = mcp.get("toolName") or mcp.get("name")
        req = mcp.get("input") or mcp.get("request")
        resp = mcp.get("output") or mcp.get("response")
        err = mcp.get("error")
    elif typ == "conversationTurn":
        ct = t.get("conversationTurn") or {}
        tool_name = ct.get("phase") or "turn"
        req = ct.get("input")
        resp = ct.get("output")
    elif typ == "sleep":
        s = t.get("sleep") or {}
        tool_name = "sleep"
        req = s
    # Fallback: accept any populated sub-object.
    if tool_name is None:
        for k in ("agentMemory", "voiceSidecar", "identity", "logMessage"):
            v = t.get(k)
            if v:
                tool_name = k
                req = v
                break

    return (
        purpose,
        tool_name,
        sierra_db.dumps(req),
        sierra_db.dumps(resp),
        err,
    )


def store_traces(conn, log_id: str, trace_events: list[dict]) -> None:
    conn.execute("DELETE FROM traces WHERE session_id=?", (log_id,))
    for idx, t in enumerate(trace_events):
        purpose, tool_name, req_json, resp_json, err = _trace_fields(t)
        conn.execute(
            "INSERT INTO traces(session_id, idx, timestamp_ms, type, purpose, tool_name, "
            "request_json, response_json, error, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log_id,
                idx,
                t.get("timestampMs"),
                t.get("type"),
                purpose,
                tool_name,
                req_json,
                resp_json,
                err,
                sierra_db.dumps(t),
            ),
        )


def store_monitors(conn, log_id: str, monitors: list[dict]) -> None:
    conn.execute("DELETE FROM monitor_results WHERE session_id=?", (log_id,))
    for m in monitors:
        conn.execute(
            "INSERT INTO monitor_results(session_id, monitor_id, slug, name, detected) "
            "VALUES (?, ?, ?, ?, ?)",
            (log_id, m.get("monitorId"), m.get("slug"), m.get("name"),
             1 if m.get("detected") else 0),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def stratified_sample(all_sessions: list[dict], target: int) -> list[dict]:
    """Pick `target` sessions distributed across duration buckets so no single
    bucket dominates analysis. Shorter-but-interesting drops get representation
    alongside longer multi-turn conversations."""
    import random
    rng = random.Random(42)

    # Bucket definitions: (label, predicate, target_share)
    buckets = [
        ("drop",     lambda s: (s.get("duration") or 0) < 5,                              0.10),
        ("frustr",   lambda s: 5  <= (s.get("duration") or 0) < 30,                       0.15),
        ("short",    lambda s: 30 <= (s.get("duration") or 0) < 120,                      0.25),
        ("medium",   lambda s: 120 <= (s.get("duration") or 0) < 300,                     0.30),
        ("deep",     lambda s: 300 <= (s.get("duration") or 0) < 600,                     0.15),
        ("long",     lambda s: (s.get("duration") or 0) >= 600,                           0.05),
    ]
    picked: list[dict] = []
    consumed: set[str] = set()
    for label, pred, share in buckets:
        pool = [s for s in all_sessions if pred(s) and s["id"] not in consumed]
        n = round(target * share)
        take = rng.sample(pool, min(n, len(pool))) if pool else []
        logger.info("  sample bucket %-8s target=%-3d available=%-4d picked=%d",
                    label, n, len(pool), len(take))
        for s in take:
            consumed.add(s["id"])
        picked.extend(take)
    # If we fell short (small bucket), top up randomly from remaining.
    if len(picked) < target:
        rest = [s for s in all_sessions if s["id"] not in consumed]
        picked.extend(rng.sample(rest, min(target - len(picked), len(rest))))
    return picked[:target]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="YYYY-MM-DD (inclusive, local midnight)")
    ap.add_argument("--until", help="YYYY-MM-DD (inclusive, end-of-day local)")
    ap.add_argument("--today", action="store_true", help="Since midnight local")
    ap.add_argument("--max",   type=int, default=None, help="Cap number of sessions (raw)")
    ap.add_argument("--sample", type=int, default=None,
                    help="After listing, pick N stratified by duration")
    ap.add_argument("--list-only", action="store_true",
                    help="Only fetch the session list (skip detail/traces)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip listing; pick sessions from DB missing details")
    args = ap.parse_args()

    sierra_db.init_db()
    client = SierraClient()

    if args.resume:
        # Skip listing; work from whatever the DB already has.
        with sierra_db.connect() as conn:
            rows = conn.execute(
                "SELECT id, timestamp_epoch AS timestamp, duration_seconds AS duration, "
                "message_count AS messageCount, device FROM sessions "
                "WHERE NOT EXISTS (SELECT 1 FROM session_details d WHERE d.id = sessions.id) "
                "ORDER BY timestamp_epoch DESC"
            ).fetchall()
            sessions = [dict(r) for r in rows]
        logger.info("Resume mode: %d sessions in DB lacking details", len(sessions))
    else:
        time_range = build_time_range(args)
        logger.info(
            "Time range: %s  →  %s",
            iso(time_range["startSeconds"]) if time_range else "any",
            iso(time_range["endSeconds"])   if time_range else "any",
        )
        sessions = fetch_session_list(client, time_range, args.max)
        with sierra_db.connect() as conn:
            for node in sessions:
                store_session_row(conn, node)
        logger.info("Stored %d session rows.", len(sessions))

    if args.sample:
        logger.info("Stratified sample: target=%d", args.sample)
        sessions = stratified_sample(sessions, args.sample)
        logger.info("Selected %d sessions after sampling", len(sessions))

    if args.list_only:
        return 0

    for i, node in enumerate(sessions, 1):
        log_id = node["id"]
        try:
            log = fetch_log_detail(client, log_id)
            traces = fetch_log_traces(client, log_id)
            monitors = fetch_monitor_results(client, log_id)
        except Exception as e:
            logger.error("  session %s failed: %r", log_id, e)
            continue
        with sierra_db.connect() as conn:
            store_session_detail(conn, log_id, log)
            store_traces(conn, log_id, traces)
            store_monitors(conn, log_id, monitors)
        logger.info(
            "  [%d/%d] %s  msgs=%d  traces=%d  monitors=%d",
            i, len(sessions), log_id,
            len(log.get("entries") or []),
            len(traces),
            len(monitors),
        )
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
