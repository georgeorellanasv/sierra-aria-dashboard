"""
Generate reports/transaction_status_improvements.md.

Cross-references Sonnet classifications against the actual agent journey
blocks for `Check Order Status` + `Check Order ETA` + related flows, and
proposes specific edits grounded in the pain points observed.

Sections:
  1. Executive summary
  2. Sessions by category + severity (from classifications)
  3. Transaction-status deep dive:
     - Pain points ranked by frequency
     - Tool-call patterns (what tools ran vs. what should have run)
     - KB articles the agent could have surfaced
     - Block-level MD excerpt + proposed changes

Run: python scripts/analyze_transaction_status.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config

REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "transaction_status_improvements.md"
TRANSACTION_LIKE = (
    "transaction_status", "cancel_transaction", "refund", "authentication",
)


def main() -> int:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    n_sessions    = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_details     = conn.execute("SELECT COUNT(*) FROM session_details").fetchone()[0]
    n_classified  = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]

    if n_classified == 0:
        print("No classifications yet. Run scripts via sierra_classifier first.",
              file=sys.stderr)
        return 1

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    a = out.append

    # -------- 1. Exec summary -----------------------------------------------
    a("# Sierra / Aria — Análisis de `Check Order Status` (transaction status)")
    a("")
    a(f"*Generado: {datetime.now().isoformat(timespec='seconds')}*  ")
    a(f"Dataset: **{n_sessions}** sesiones listadas · **{n_details}** con detalle · **{n_classified}** clasificadas por Sonnet.")
    a("")

    # -------- 2. By category + severity -------------------------------------
    a("## 1. Distribución por categoría × severidad")
    a("")
    rows = conn.execute("""
        SELECT category, severity, COUNT(*) n
        FROM classifications
        GROUP BY category, severity
        ORDER BY category, severity
    """).fetchall()
    categories = sorted({r["category"] for r in rows if r["category"]})
    severities = ["critical", "high", "medium", "low"]
    # Build a pivot
    grid = defaultdict(lambda: defaultdict(int))
    for r in rows:
        grid[r["category"]][r["severity"]] = r["n"]
    a(f"| Categoría | {' | '.join(s.title() for s in severities)} | **Total** |")
    a("|---|" + "---|" * len(severities) + "---|")
    for cat in categories:
        tot = sum(grid[cat].values())
        a(f"| {cat} | " + " | ".join(str(grid[cat].get(s, 0)) for s in severities) + f" | **{tot}** |")
    a("")

    # -------- 3. Pain-point frequency (across all categories) ---------------
    a("## 2. Top pain points (todas las categorías, agrupados)")
    a("")
    all_points: list[str] = []
    for r in conn.execute("SELECT pain_points_json FROM classifications WHERE pain_points_json IS NOT NULL"):
        try:
            pts = json.loads(r["pain_points_json"])
            if isinstance(pts, list):
                all_points.extend(str(p) for p in pts)
        except json.JSONDecodeError:
            pass
    pp_counter = Counter(all_points)
    a("| # | Pain point | Frecuencia |")
    a("|---|---|---|")
    for i, (pt, n) in enumerate(pp_counter.most_common(25), 1):
        a(f"| {i} | {pt} | {n} |")
    a("")

    # -------- 4. Transaction-status deep dive -------------------------------
    a("## 3. Deep-dive — `transaction_status` + flujos relacionados")
    a("")
    ts_rows = conn.execute(f"""
        SELECT c.session_id, c.category, c.subcategory, c.severity,
               c.pain_points_json, c.suggestion, c.related_journey_blocks,
               c.related_kb_articles,
               s.duration_seconds, s.device, s.first_user_message
        FROM classifications c
        JOIN sessions s ON s.id = c.session_id
        WHERE c.category IN ({','.join(['?']*len(TRANSACTION_LIKE))})
        ORDER BY
          CASE c.severity
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 ELSE 3 END,
          s.duration_seconds DESC
    """, TRANSACTION_LIKE).fetchall()
    a(f"**{len(ts_rows)}** sesiones caen en categorías transaccionales (transaction_status / cancel_transaction / refund / authentication).")
    a("")

    # Most-named journey blocks by the classifier
    block_counter: Counter[str] = Counter()
    block_sessions: defaultdict[str, list[str]] = defaultdict(list)
    for r in ts_rows:
        try:
            blocks = json.loads(r["related_journey_blocks"] or "[]")
        except json.JSONDecodeError:
            blocks = []
        for b in blocks:
            block_counter[str(b)] += 1
            block_sessions[str(b)].append(r["session_id"])
    if block_counter:
        a("### 3.1 Journey blocks más implicados")
        a("")
        a("| Block | Sesiones |")
        a("|---|---|")
        for name, n in block_counter.most_common(10):
            a(f"| {name} | {n} |")
        a("")

    # Tool-call pattern across ts sessions
    a("### 3.2 Tools invocados en sesiones transaccionales")
    a("")
    sess_ids = [r["session_id"] for r in ts_rows]
    if sess_ids:
        placeholders = ",".join(["?"] * len(sess_ids))
        framework = ("goalsdk_respond", "ask_ai", "sleep", "should_query_kb",
                     "deadlock_detector", "detect_abuse", "param_validation", "turn")
        fp = ",".join(["?"] * len(framework))
        tool_rows = conn.execute(
            f"""SELECT tool_name, COUNT(*) n FROM traces
                WHERE session_id IN ({placeholders})
                  AND tool_name IS NOT NULL
                  AND tool_name NOT IN ({fp})
                GROUP BY tool_name ORDER BY n DESC LIMIT 15""",
            (*sess_ids, *framework),
        ).fetchall()
        a("| Tool | Llamadas |")
        a("|---|---|")
        for r in tool_rows:
            a(f"| `{r['tool_name']}` | {r['n']} |")
        a("")

    # -------- 5. Top suggestions verbatim -----------------------------------
    a("### 3.3 Sugerencias concretas de Sonnet (top 15)")
    a("")
    sugg_counter: Counter[str] = Counter()
    for r in ts_rows:
        s = (r["suggestion"] or "").strip()
        if s:
            sugg_counter[s] += 1
    for i, (s, n) in enumerate(sugg_counter.most_common(15), 1):
        a(f"{i}. **({n}×)** {s}")
    a("")

    # -------- 6. Current journey blocks MD (for reference) ------------------
    a("## 4. Contenido actual de los journey blocks de referencia")
    a("")
    for name in ("Check Order Status", "Check Order ETA", "Select Order",
                 "Cancel Customer Order", "Intents Where User Needs to Authenticate"):
        r = conn.execute(
            "SELECT block_name, block_type, markdown FROM journey_blocks "
            "WHERE block_name = ? ORDER BY LENGTH(markdown) DESC LIMIT 1",
            (name,),
        ).fetchone()
        if not r:
            continue
        a(f"### 4.{name}")
        a(f"*type: `{r['block_type']}`*")
        a("")
        a("```markdown")
        md = (r["markdown"] or "").strip()
        # Cap to 2500 chars for readability
        a(md[:2500] + ("…" if len(md) > 2500 else ""))
        a("```")
        a("")

    # -------- 7. Related KB articles candidates -----------------------------
    a("## 5. KB articles candidatos a reforzar")
    a("")
    kb_mentions: Counter[str] = Counter()
    for r in ts_rows:
        try:
            arts = json.loads(r["related_kb_articles"] or "[]")
        except json.JSONDecodeError:
            arts = []
        for t in arts:
            kb_mentions[str(t)] += 1
    if kb_mentions:
        a("| Artículo (por título) | Menciones |")
        a("|---|---|")
        for title, n in kb_mentions.most_common(15):
            a(f"| {title} | {n} |")
        a("")
    else:
        a("*No hubo menciones específicas de artículos en las clasificaciones — considerar si la KB está incompleta.*")
        a("")

    # -------- 8. Sample critical/high sessions for human review -------------
    a("## 6. Sesiones críticas / high para revisión humana")
    a("")
    crit = [r for r in ts_rows if r["severity"] in ("critical", "high")][:10]
    for r in crit:
        a(f"- **{r['session_id']}** — `{r['category']}` · `{r['severity']}` · "
          f"{r['duration_seconds']}s · *{(r['first_user_message'] or '')[:80]}*")
        try:
            pts = json.loads(r["pain_points_json"] or "[]")
            for p in pts[:3]:
                a(f"  - {p}")
        except json.JSONDecodeError:
            pass
        if r["suggestion"]:
            a(f"  - **Sugerencia:** {r['suggestion']}")
        a("")

    # -------- Write file ----------------------------------------------------
    REPORT_PATH.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {REPORT_PATH}  ({REPORT_PATH.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
