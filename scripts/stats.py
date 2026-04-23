"""
Quick exploratory stats on the scraped Sierra data — no LLM required.
Use this first to sanity-check the scrape and spot obvious patterns before
running the Sonnet classifier.

Prints to stdout:
  - totals
  - sessions by duration / message bucket
  - top tags
  - top tool calls (from traces)
  - monitor detection rates
  - outcomes ('agent transferred' / 'call disconnected' / etc.)

Usage: python scripts/stats.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config


def main() -> int:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    q = lambda sql, *a: list(conn.execute(sql, a))  # noqa: E731

    def section(title: str):
        print(f"\n{'='*10} {title} {'='*(60-len(title))}")

    section("Totals")
    for tbl in ("sessions", "session_details", "messages", "traces",
                "monitor_results", "session_tags", "journeys", "journey_blocks",
                "tools", "kb_sources", "kb_articles", "classifications"):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl:<22} {n}")
        except sqlite3.OperationalError:
            pass

    section("Duration x message buckets")
    rows = q("""
        SELECT
            CASE
                WHEN duration_seconds IS NULL THEN 'null'
                WHEN duration_seconds < 5 THEN '0-4s'
                WHEN duration_seconds < 30 THEN '5-29s'
                WHEN duration_seconds < 60 THEN '30-59s'
                WHEN duration_seconds < 180 THEN '1-3m'
                WHEN duration_seconds < 600 THEN '3-10m'
                ELSE '10m+' END dur,
            COUNT(*) n
        FROM sessions GROUP BY dur
    """)
    for r in sorted(rows, key=lambda x: x["n"], reverse=True):
        print(f"  {r['dur']:<10} {r['n']}")

    section("Review status")
    for r in q("SELECT review_status, COUNT(*) n FROM sessions GROUP BY review_status ORDER BY n DESC"):
        print(f"  {r['review_status'] or 'null':<20} {r['n']}")

    section("Top 25 tags (from session_tags)")
    for r in q("""SELECT tag, COUNT(*) n FROM session_tags
                  GROUP BY tag ORDER BY n DESC LIMIT 25"""):
        print(f"  {r['n']:>5}  {r['tag']}")

    section("Top 20 external tools called (from traces, excludes framework tasks)")
    framework = ("goalsdk_respond", "ask_ai", "sleep", "should_query_kb",
                 "deadlock_detector", "detect_abuse", "param_validation", "turn")
    placeholders = ",".join(["?"] * len(framework))
    for r in q(f"""SELECT tool_name, COUNT(*) n FROM traces
                   WHERE tool_name IS NOT NULL AND tool_name NOT IN ({placeholders})
                   GROUP BY tool_name ORDER BY n DESC LIMIT 20""", *framework):
        print(f"  {r['n']:>5}  {r['tool_name']}")

    section("Monitor detection rate")
    for r in q("""SELECT name,
                         SUM(detected) AS detected,
                         COUNT(*) AS total,
                         ROUND(100.0*SUM(detected)/COUNT(*), 1) AS pct
                  FROM monitor_results
                  GROUP BY name ORDER BY pct DESC"""):
        print(f"  {r['name']:<25} {r['detected']:>5}/{r['total']:<5}  ({r['pct']}%)")

    section("Sessions with transfer-to-human (tag 'transferred' or similar)")
    for r in q("""SELECT COUNT(DISTINCT s.id) n FROM sessions s
                  JOIN session_tags t ON t.session_id = s.id
                  WHERE t.tag LIKE '%transfer%' OR t.tag LIKE '%escalat%' OR t.tag LIKE '%human%' """):
        print(f"  {r['n']}")

    section("Top 15 'first user messages' (exact text)")
    for r in q("""SELECT first_user_message, COUNT(*) n FROM sessions
                  WHERE first_user_message IS NOT NULL AND first_user_message != ''
                  GROUP BY first_user_message ORDER BY n DESC LIMIT 15"""):
        text = r['first_user_message'] or ''
        print(f"  {r['n']:>4}  {text[:80]}")

    section("Journey blocks and their sizes")
    for r in q("""SELECT block_name, block_type, LENGTH(markdown) AS md_len
                  FROM journey_blocks WHERE block_name != '' ORDER BY md_len DESC"""):
        print(f"  {r['block_name']:<50}  {r['block_type']:<20}  {r['md_len']:>6} chars")

    print()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
