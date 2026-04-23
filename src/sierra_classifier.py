"""
Sonnet-4.6 classifier for Sierra sessions.

For each unclassified session, builds a prompt from:
  - [cached] System instructions (taxonomy + output schema)
  - [cached] Agent context: journey blocks MD + tool descriptions + KB titles
  - [per-session] Transcript + tool traces + tags + monitor hits

Returns structured JSON: category, subcategory, pain_points, severity,
suggestion, related_journey_blocks, related_kb_articles.

Uses Anthropic prompt caching — the agent context is ~50-100KB and stays
constant across all sessions in a run, so the cache hit saves ~$10 per 1000
sessions.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from src import config, sierra_db

logger = logging.getLogger("classifier")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1200

SYSTEM_PROMPT = """You review Sierra AI voice-agent sessions for Ria Money \
Transfer and flag what the agent could do better.

TAXONOMY — pick exactly one `category`:
  - transaction_status : caller asked where their transfer is, order status, ETA
  - cancel_transaction : caller wants to cancel an order
  - refund             : caller wants a refund
  - authentication     : caller needs CVP/identity verification or re-auth
  - transfer_to_human  : caller wants/needs a live agent
  - general_info       : fees, hours, branch locations, app help
  - technical_issue    : app crash, login problem, PIN reset, card/wallet issue
  - complaint          : frustration, escalation of prior issue
  - greeting_drop      : caller hung up at greeting / no-intent
  - other              : anything else

`severity` guidance:
  - critical : agent failed silently, caller left unresolved with money at stake
  - high     : caller frustrated, wrong info given, or unnecessary transfer to human
  - medium   : friction but eventually resolved; agent repeated questions, slow, confused
  - low      : minor, cosmetic, or expected drop (test call, greeting-only)

`pain_points` : list of short (<15 words) concrete issues, e.g.:
  [
    "Agent asked for order number twice even though user gave it",
    "Transferred to human without attempting transaction lookup"
  ]

`suggestion` : one specific, actionable change to a journey block or tool. \
Reference block names and tool names when possible. No generic advice.

`related_journey_blocks` : names of journey blocks (from the list provided) \
most relevant to this session.

`related_kb_articles` : up to 5 KB article titles most relevant (can be empty).

Output ONLY a JSON object matching this schema. No prose before or after."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def load_agent_context(conn: sqlite3.Connection) -> str:
    """
    Build the static context that every classification sees.
    Cache this as a single large block so Anthropic's prompt cache amortises it.
    """
    parts: list[str] = []

    # Journeys + blocks
    parts.append("# Journey blocks (agent instructions)\n")
    for r in conn.execute(
        "SELECT block_name, block_type, markdown FROM journey_blocks "
        "WHERE block_name != '' ORDER BY block_name"
    ):
        parts.append(f"## {r['block_name']}  ({r['block_type']})\n\n{r['markdown']}\n")

    # Tools
    parts.append("\n# Tools available to the agent\n")
    for r in conn.execute(
        "SELECT name, description FROM tools ORDER BY name"
    ):
        parts.append(f"- **{r['name']}** — {r['description'] or ''}")

    # KB article titles only (not content — too big; titles help the model reference them)
    parts.append("\n\n# Knowledge base article titles\n")
    for r in conn.execute(
        "SELECT title FROM kb_articles WHERE title IS NOT NULL ORDER BY title"
    ):
        parts.append(f"- {r['title']}")

    return "\n".join(parts)


def build_session_payload(conn: sqlite3.Connection, session_id: str) -> str:
    """Return the per-session text block (the variable part of the prompt)."""
    parts: list[str] = [f"# Session {session_id}\n"]

    s = conn.execute(
        "SELECT timestamp_iso, duration_seconds, device, review_status, "
        "message_count, first_user_message FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    if s:
        parts.append(
            f"- timestamp: {s['timestamp_iso']}\n"
            f"- duration_s: {s['duration_seconds']}\n"
            f"- device: {s['device']}\n"
            f"- review_status: {s['review_status']}\n"
            f"- message_count: {s['message_count']}\n"
            f"- first_user_message: {s['first_user_message']!r}\n"
        )

    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM session_tags WHERE session_id=? ORDER BY tag", (session_id,))]
    if tags:
        parts.append("## Tags\n" + ", ".join(tags))

    monitors = [
        f"{r['name']}: {'DETECTED' if r['detected'] else 'ok'}"
        for r in conn.execute(
            "SELECT name, detected FROM monitor_results WHERE session_id=? ORDER BY name",
            (session_id,))
    ]
    if monitors:
        parts.append("## Monitors\n" + "\n".join(f"- {m}" for m in monitors))

    # Transcript (only role + text, skip empty system events)
    parts.append("\n## Transcript")
    for r in conn.execute(
        "SELECT role, text FROM messages WHERE session_id=? AND text IS NOT NULL "
        "AND text != '' ORDER BY idx", (session_id,)
    ):
        role = (r["role"] or "?").upper()
        parts.append(f"**{role}:** {r['text']}")

    # Tool calls summary (what the agent actually did)
    tool_calls = list(conn.execute(
        "SELECT tool_name, type, COUNT(*) n FROM traces WHERE session_id=? "
        "AND tool_name IS NOT NULL AND tool_name NOT IN "
        "('goalsdk_respond','ask_ai','sleep','should_query_kb','deadlock_detector',"
        "'detect_abuse','param_validation','turn') "
        "GROUP BY tool_name, type ORDER BY n DESC", (session_id,)))
    if tool_calls:
        parts.append("\n## Tool calls")
        for r in tool_calls:
            parts.append(f"- {r['tool_name']} ({r['type']}) × {r['n']}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class Classifier:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or config.ANTHROPIC_API_KEY
        if not key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is required for classification; set it in .env"
            )
        self.client = Anthropic(api_key=key)

    def classify(self, agent_context: str, session_payload: str) -> dict:
        resp = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": agent_context,
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": session_payload}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        # Strip code fences if any, then parse JSON.
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip("` \n")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Model returned non-JSON: {text[:500]}")
        data["_usage"] = {
            "input_tokens":  resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read":    getattr(resp.usage, "cache_read_input_tokens", 0),
            "cache_write":   getattr(resp.usage, "cache_creation_input_tokens", 0),
        }
        return data


def store_classification(conn: sqlite3.Connection, session_id: str, result: dict) -> None:
    sierra_db.upsert(conn, "classifications", {
        "session_id":              session_id,
        "model":                   MODEL,
        "category":                result.get("category"),
        "subcategory":             result.get("subcategory"),
        "pain_points_json":        sierra_db.dumps(result.get("pain_points")),
        "severity":                result.get("severity"),
        "suggestion":              result.get("suggestion"),
        "related_journey_blocks":  sierra_db.dumps(result.get("related_journey_blocks")),
        "related_kb_articles":     sierra_db.dumps(result.get("related_kb_articles")),
        "raw_response_json":       sierra_db.dumps(result),
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="Classify this session_id only")
    ap.add_argument("--limit", type=int, help="Max sessions to classify this run")
    ap.add_argument("--redo", action="store_true",
                    help="Re-classify sessions already in classifications table")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s")

    classifier = Classifier()

    with sierra_db.connect() as conn:
        agent_ctx = load_agent_context(conn)
        logger.info("Agent context: %d chars", len(agent_ctx))

        where = ""
        params: tuple = ()
        if args.only:
            where = "WHERE s.id = ?"
            params = (args.only,)
        elif not args.redo:
            where = "WHERE NOT EXISTS (SELECT 1 FROM classifications c WHERE c.session_id = s.id)"

        sql = (
            "SELECT s.id FROM sessions s "
            "JOIN session_details d ON d.id = s.id "
            f"{where} "
            "ORDER BY s.timestamp_epoch DESC"
        )
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        session_ids = [r["id"] for r in conn.execute(sql, params)]

    logger.info("Sessions to classify: %d", len(session_ids))

    total_in = total_out = total_cache_read = total_cache_write = 0

    for i, sid in enumerate(session_ids, 1):
        with sierra_db.connect() as conn:
            payload = build_session_payload(conn, sid)

        t0 = time.monotonic()
        try:
            result = classifier.classify(agent_ctx, payload)
        except Exception as e:
            logger.error("  session %s FAILED: %r", sid, e)
            continue
        dt = time.monotonic() - t0

        u = result.pop("_usage", {})
        total_in += u.get("input_tokens", 0)
        total_out += u.get("output_tokens", 0)
        total_cache_read += u.get("cache_read", 0)
        total_cache_write += u.get("cache_write", 0)

        with sierra_db.connect() as conn:
            store_classification(conn, sid, result)

        logger.info(
            "  [%d/%d] %s  cat=%s  sev=%s  in=%d out=%d cache(r/w)=%d/%d  (%.1fs)",
            i, len(session_ids), sid,
            result.get("category"), result.get("severity"),
            u.get("input_tokens", 0), u.get("output_tokens", 0),
            u.get("cache_read", 0), u.get("cache_write", 0),
            dt,
        )

    logger.info(
        "Tokens total — in:%d  out:%d  cache-read:%d  cache-write:%d",
        total_in, total_out, total_cache_read, total_cache_write,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
