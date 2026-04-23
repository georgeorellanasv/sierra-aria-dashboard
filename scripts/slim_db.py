"""
Slim sierra.db for deployment — NULLs the fat raw-JSON columns so the file
fits under GitHub's 100 MB limit. The dashboard doesn't display these raw
blobs — only their parsed counterpart columns — so nothing visible breaks.

  traces.raw_json            (~264 MB) -> NULL
  traces.request_json        (~226 MB) -> NULL (kept: type, tool_name, error)
  session_details.raw_log_json (~177 MB) -> NULL
  messages.raw_json          (~2.5 MB) -> NULL
  sessions.raw_list_json     (~6.9 MB) -> NULL

Run once before deploying:
  python scripts/slim_db.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "sierra.db"


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}")
        return 1
    before = DB.stat().st_size / 1024 / 1024
    print(f"Before: {before:.1f} MB")

    conn = sqlite3.connect(DB)
    conn.isolation_level = None
    cur = conn.cursor()

    cur.execute("UPDATE traces           SET raw_json     = NULL WHERE raw_json     IS NOT NULL")
    print(f"  traces.raw_json nulled ({cur.rowcount} rows)")
    cur.execute("UPDATE traces           SET request_json = NULL WHERE request_json IS NOT NULL")
    print(f"  traces.request_json nulled ({cur.rowcount} rows)")
    cur.execute("UPDATE session_details  SET raw_log_json = NULL WHERE raw_log_json IS NOT NULL")
    print(f"  session_details.raw_log_json nulled ({cur.rowcount} rows)")
    cur.execute("UPDATE messages         SET raw_json     = NULL WHERE raw_json     IS NOT NULL")
    print(f"  messages.raw_json nulled ({cur.rowcount} rows)")
    cur.execute("UPDATE sessions         SET raw_list_json = NULL WHERE raw_list_json IS NOT NULL")
    print(f"  sessions.raw_list_json nulled ({cur.rowcount} rows)")

    print("Running VACUUM...")
    cur.execute("VACUUM")
    conn.close()

    after = DB.stat().st_size / 1024 / 1024
    print(f"After:  {after:.1f} MB  ({100 * (1 - after/before):.0f}% smaller)")
    if after > 95:
        print("⚠️  Still > 95 MB — may need Git LFS or further slim.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
