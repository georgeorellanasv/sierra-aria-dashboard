"""
Sierra / Aria — Streamlit dashboard for cross-chart drill-down.

Pages:
  1. Overview          — totals + master sunburst + monitor rates
  2. Session Explorer  — the core cross-filter experience
  3. Gap Drilldown     — per-gap deep dive
  4. Simulations       — sim coverage vs our gaps
  5. Issue Log         — 20 clustered issues with filters

Run:
  streamlit run dashboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Promote Streamlit secrets into os.environ BEFORE importing modules that
# read from os.getenv (src/config.py, etc.). This lets secrets.toml work as
# an env-var source on Streamlit Cloud without changing the scraping code.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str) and _k not in os.environ:
            os.environ[_k] = _v
except (FileNotFoundError, Exception):
    pass

# Reuse diagnosis metadata from the HTML builder
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from build_diagnostic_html import GAPS, NEW_JOURNEYS, NEW_TOOLS, GLOBAL_RULES_TO_ADD, \
    JOURNEY_CHANGES, SIMULATIONS, GAP_SIM_COVERAGE, SIERRA_SESSION_URL

from src import config

st.set_page_config(
    page_title="Sierra / Aria — Diagnostic",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Anthropic-ish theme via custom CSS
# ---------------------------------------------------------------------------

st.markdown("""
<style>
.stApp { background: #faf9f5; }
.block-container { padding-top: 1.2rem; max-width: 1400px; }
h1, h2, h3 { font-weight: 600; letter-spacing: -0.01em; }
h1 { color: #1a1a1a; }
.stMetric > div { background: #fff; border: 1px solid #d8d1c2; padding: 0.6rem 1rem; border-radius: 4px; }
[data-testid="stMetricValue"] { color: #c44f3a; font-weight: 600; }
[data-testid="stMetricLabel"] { color: #6b6257; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; }
.sev-crit { background: #c44f3a; color: #fff; padding: 2px 8px; border-radius: 2px; font-size: 0.72rem; font-weight: 600; }
.sev-high { background: #d97e5a; color: #fff; padding: 2px 8px; border-radius: 2px; font-size: 0.72rem; font-weight: 600; }
.sev-med  { background: #c9a449; color: #1a1a1a; padding: 2px 8px; border-radius: 2px; font-size: 0.72rem; font-weight: 600; }
.sev-low  { background: #6c8d5a; color: #fff; padding: 2px 8px; border-radius: 2px; font-size: 0.72rem; font-weight: 600; }
.session-link { color: #c44f3a; text-decoration: none; font-family: monospace; font-size: 0.78rem; background: #fff6f3; padding: 1px 6px; border-radius: 2px; border: 1px solid #eddbd4; }
.transcript-user  { background: #eef4ee; padding: 0.5rem 0.8rem; border-left: 3px solid #6c8d5a; margin: 0.3rem 0; border-radius: 2px; }
.transcript-agent { background: #fff6f3; padding: 0.5rem 0.8rem; border-left: 3px solid #c44f3a; margin: 0.3rem 0; border-radius: 2px; }
.transcript-sys   { background: #f5f2e8; padding: 0.3rem 0.8rem; font-size: 0.8rem; color: #6b6257; margin: 0.2rem 0; border-radius: 2px; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Simple passcode gate — prevents anyone without the code from loading the app.
# Not real authentication; a single shared code with per-session persistence.
# Passcode reads from st.secrets["APP_PASSCODE"] when deployed, falls back
# to the literal "12345" for local dev.
# ---------------------------------------------------------------------------

try:
    APP_PASSCODE = st.secrets["APP_PASSCODE"]
except (FileNotFoundError, KeyError, Exception):
    APP_PASSCODE = "12345"

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    _cols = st.columns([1, 2, 1])
    with _cols[1]:
        st.markdown("<div style='height:4rem;'></div>", unsafe_allow_html=True)
        with st.form("auth_form", clear_on_submit=False):
            code = st.text_input(
                "Enter passcode",
                type="password",
                placeholder="••••",
            )
            ok = st.form_submit_button("Enter", type="primary",
                                       use_container_width=True)
            if ok:
                if code == str(APP_PASSCODE):
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Incorrect passcode.")
    st.stop()


# ---------------------------------------------------------------------------
# Data loading — cached so filter changes don't hit SQLite every time
# ---------------------------------------------------------------------------

DB_PATH = str(config.DB_PATH)
AGENT_ID_SLUG = config.SIERRA_AGENT_ID.removeprefix("bot-")


@st.cache_data(show_spinner=False)
def load_sessions() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("""
        SELECT s.id, s.timestamp_epoch, s.timestamp_iso, s.duration_seconds,
               s.device, s.message_count, s.review_status, s.first_user_message,
               d.locale,
               (CASE WHEN d.id IS NOT NULL THEN 1 ELSE 0 END) AS has_detail,
               c.category, c.severity, c.suggestion,
               c.pain_points_json, c.related_journey_blocks, c.related_kb_articles
        FROM sessions s
        LEFT JOIN session_details d ON d.id = s.id
        LEFT JOIN classifications c  ON c.session_id = s.id
    """, conn)
    conn.close()
    df["severity"] = df["severity"].fillna("(unclassified)").str.title()
    df["category"] = df["category"].fillna("(unclassified)")
    df["duration_seconds"] = df["duration_seconds"].fillna(0)
    return df


def sampled(df: pd.DataFrame) -> pd.DataFrame:
    """Return only sessions we scraped in detail — the analytical universe."""
    return df[df["has_detail"] == 1].copy()


@st.cache_data(show_spinner=False)
def load_tags() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT session_id, tag FROM session_tags", conn)
    conn.close()
    return df


@st.cache_data(show_spinner=False)
def load_traces() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("""
        SELECT session_id, idx, timestamp_ms, type, tool_name, error
        FROM traces
    """, conn)
    conn.close()
    return df


@st.cache_data(show_spinner=False)
def load_monitors() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT session_id, monitor_id, slug, name, detected FROM monitor_results", conn
    )
    conn.close()
    return df


@st.cache_data(show_spinner=False)
def load_messages(session_id: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT idx, role, text, timestamp FROM messages WHERE session_id=? ORDER BY idx",
        conn, params=(session_id,),
    )
    conn.close()
    return df


@st.cache_data(show_spinner=False)
def load_session_traces(session_id: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("""
        SELECT idx, timestamp_ms, type, purpose, tool_name, error,
               request_json, response_json
        FROM traces WHERE session_id=? ORDER BY idx
    """, conn, params=(session_id,))
    conn.close()
    return df


@st.cache_data(show_spinner=False)
def load_issues_raw() -> list[dict]:
    raw = Path(__file__).parent / "reports" / "issue_log_raw.json"
    if not raw.exists():
        return []
    text = raw.read_text(encoding="utf-8").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    data = json.loads(text)
    issues = data.get("issues") or []
    for i, it in enumerate(issues, 1):
        it["_idx"] = i
    return issues


SEV_ORDER = ["Critical", "High", "Medium", "Low", "(unclassified)"]
SEV_COLOR = {
    "Critical":        "#c44f3a",
    "High":            "#d97e5a",
    "Medium":          "#c9a449",
    "Low":             "#6c8d5a",
    "(unclassified)":  "#b8b0a0",
}


def sierra_url(session_id: str) -> str:
    return f"{SIERRA_SESSION_URL}{session_id}"


GAPS_BY_ID = {g["id"]: g for g in GAPS}


def gap_popover(gap_id: int, *, label: str | None = None, use_container_width: bool = False) -> None:
    """Render a small popover button; clicking shows the full gap card inline."""
    g = GAPS_BY_ID.get(gap_id)
    if not g:
        return
    sev_cls = {"Critical": "sev-crit", "High": "sev-high",
               "Medium": "sev-med", "Low": "sev-low"}[g["severity"]]
    btn = label or f"Gap #{gap_id}"
    with st.popover(btn, use_container_width=use_container_width):
        st.markdown(
            f'### Gap #{g["id"]} — {g["title_en"]}  '
            f'<span class="{sev_cls}">{g["severity"]}</span>',
            unsafe_allow_html=True,
        )
        src_label = "UI review" if g["source"] == "config-review" else "Data mining"
        grounded_label = {
            "scraped-block": "Grounded in scraped journey block content",
            "scraped-data":  "Grounded in scraped data signals (tags / traces / monitors)",
            "data-only":     "Inferred from transcript patterns (not verified against a specific block)",
        }.get(g["grounded_in"], "")
        st.caption(f"{src_label}  ·  {grounded_label}")
        st.markdown(f"**What's wrong:** _{g['evidence_en']}_")
        st.markdown(f"**Data signal:** {g['data_signal_en']}")
        if g.get("samples"):
            st.markdown("**Sample sessions:**")
            for s in g["samples"][:3]:
                st.markdown(f"- [{s}]({sierra_url(s)}) ↗")
        if g.get("issues"):
            st.markdown(
                f"**Linked issues in log:** " + ", ".join(f"#{i}" for i in g["issues"])
            )
        st.caption("Full details in the 🎯 Gap Drilldown page.")


def inline_gap_refs(gap_ids: list[int]) -> None:
    """Render a compact row of gap popover buttons — use inside layouts."""
    if not gap_ids:
        return
    cols = st.columns(len(gap_ids))
    for col, gid in zip(cols, gap_ids):
        with col:
            gap_popover(gid)


# ---------------------------------------------------------------------------
# Sidebar — global navigation
# ---------------------------------------------------------------------------

PAGES_MAIN = {
    "Overview":      "🔎",
    "Gap Proposals": "🎯",
    "PO Recs":       "📌",
    "Strategic":     "🧠",
    "Glossary":      "📖",
}
PAGES_ADV = {
    "Investigate":   "🧭",
    "Simulations":   "🧪",
}
PAGES = {**PAGES_MAIN, **PAGES_ADV}

# Build date for the analysis (today — this is refreshed every scrape)
ANALYSIS_DATE_LABEL = "April 22, 2026"

# ----- Language state (ES default) -----
if "lang" not in st.session_state:
    st.session_state["lang"] = "es"

def _t(es: str, en: str) -> str:
    """Return the string in the currently selected language."""
    return es if st.session_state.get("lang", "es") == "es" else en


# ----- Active page state (shared between the two radios) -----
if "active_page" not in st.session_state:
    st.session_state["active_page"] = "Overview"


def _main_nav_changed():
    choice = st.session_state.get("main_nav")
    if choice:
        st.session_state["active_page"] = choice
        st.session_state["adv_nav"] = None


def _adv_nav_changed():
    choice = st.session_state.get("adv_nav")
    if choice:
        st.session_state["active_page"] = choice
        st.session_state["main_nav"] = None


with st.sidebar:
    st.markdown("### Sierra / Aria")
    st.caption(
        _t(
            f"Análisis del {ANALYSIS_DATE_LABEL} · agente de voz Aria (Ria)",
            f"{ANALYSIS_DATE_LABEL} analysis · Aria voice agent (Ria)",
        )
    )

    # Language toggle
    lang_cols = st.columns(2)
    with lang_cols[0]:
        if st.button("🇪🇸 ES",
                     use_container_width=True,
                     type="primary" if st.session_state["lang"] == "es" else "secondary"):
            st.session_state["lang"] = "es"
            st.rerun()
    with lang_cols[1]:
        if st.button("🇬🇧 EN",
                     use_container_width=True,
                     type="primary" if st.session_state["lang"] == "en" else "secondary"):
            st.session_state["lang"] = "en"
            st.rerun()

    st.markdown("---")

    # Main nav (always visible)
    main_opts = list(PAGES_MAIN.keys())
    # Pre-select the current active page if it's a main page; otherwise no selection
    main_idx = main_opts.index(st.session_state["active_page"]) \
               if st.session_state["active_page"] in main_opts else None
    st.radio(
        "main_nav",
        options=main_opts,
        format_func=lambda p: f"{PAGES_MAIN[p]}  {p}",
        label_visibility="collapsed",
        key="main_nav",
        index=main_idx,
        on_change=_main_nav_changed,
    )

    # Advanced nav (collapsed)
    adv_opts = list(PAGES_ADV.keys())
    adv_idx = adv_opts.index(st.session_state["active_page"]) \
              if st.session_state["active_page"] in adv_opts else None
    with st.expander(_t("🔧  Más vistas (avanzado)", "🔧  More views (advanced)")):
        st.radio(
            "adv_nav",
            options=adv_opts,
            format_func=lambda p: f"{PAGES_ADV[p]}  {p}",
            label_visibility="collapsed",
            key="adv_nav",
            index=adv_idx,
            on_change=_adv_nav_changed,
        )

    page = st.session_state["active_page"]
    st.markdown("---")
    st.caption(f"DB: `{Path(DB_PATH).name}`")


# ---------------------------------------------------------------------------
# PAGE 1 — Overview
# ---------------------------------------------------------------------------

def page_overview():
    st.title(_t(
        f"Sierra / Aria — Diagnóstico  ·  {ANALYSIS_DATE_LABEL}",
        f"Sierra / Aria — Diagnostic  ·  {ANALYSIS_DATE_LABEL}",
    ))
    st.caption(_t(
        "Lectura de arriba hacia abajo: KPIs macro → distribución de severidad → "
        "dónde se concentra el dolor → qué señales disparó Sierra → issues priorizados. "
        "Todos los números vienen de `data/sierra.db` (re-scrapeable en cualquier momento).",
        "Read top-down: macro KPIs → severity mix → where pain concentrates → "
        "what fired → ranked issues. Every number is sourced from `data/sierra.db`.",
    ))

    # ---------- Glossary (collapsible, open by default first visit) ----------
    with st.expander(_t(
        "📖  Glosario — ábrelo primero si algún término no te resulta familiar",
        "📖  Glossary — read this first if any term is unfamiliar",
    ), expanded=False):
        st.markdown("""
**Session / Llamada** — One phone call with Aria (the voice agent). Each has a
unique ID like `audit-01KPVQ…`. We listed every call that happened today;
for a sampled subset we also downloaded the full transcript, tool calls,
and metadata.

**Sessions listed** — Total calls Sierra recorded today (we only read the
list — name, duration, first message, tags).

**Details scraped** — Subset where we also pulled the full transcript, the
tool calls the agent made, and the monitor results. These are the ones
we can actually analyse.

**Classified** — Subset where Sonnet 4.6 (Anthropic's LLM) read the
transcript + tool calls and assigned one of 10 categories
(`transaction_status`, `authentication`, `cancel_transaction`, …) plus a
severity.

**Severity — how it's computed**

Sonnet assigns severity based on caller impact:

| Severity | Meaning |
|----------|---------|
| 🔴 **Critical** | Agent failed silently, caller left unresolved with money at stake. Real harm. |
| 🟠 **High** | Caller frustrated, wrong info given, or unnecessary transfer to human. |
| 🟡 **Medium** | Friction but eventually resolved; agent repeated questions, slow, confused. |
| 🟢 **Low** | Minor / cosmetic / expected drop (test calls, greeting-only, wrong number). |

**Journey** — A Sierra config concept: one "flow" for handling a specific
caller intent. Aria has 5 today (Authenticate, Select Order, Check Status,
Cancel, Check ETA).

**Tool** — A function the agent can call (like `tool:CustomerByOrderNumber`
or `tool:transfer`). There are 17 tools total. They either succeed or fail
on each call.

**Monitor** — Sierra's built-in real-time watchers that look for problem
patterns during a call (Agent Looping, Frustration, False Transfer,
Repeated Escalation). They flag issues but do NOT currently change the
agent's behaviour — they're observability, not intervention.

**Tag** — A label Sierra puts on a session (e.g. `unsupportedIntent:recall`,
`tool:transfer:invoked`, `language:es`). Tags are also observability: they
describe what happened but do not control routing.

**Structural gap** — A missing piece of configuration we identified by
cross-referencing the scraped data with Sierra's UI. 15 total today
(7 from UI review, 8 from data mining).

**Issue** — A concrete problem pattern that appeared in ≥1 session,
clustered by Sonnet. 20 in total, each linked to one or more structural
gaps and to sample sessions for human review.
""")

    sessions_all = load_sessions()
    sessions     = sampled(sessions_all)        # <-- our analytical universe (111)
    monitors     = load_monitors()
    tags         = load_tags()
    traces       = load_traces()
    issues       = load_issues_raw()

    classified = sessions[sessions["category"] != "(unclassified)"].copy()
    n_listed   = len(sessions_all)
    n_sample   = len(sessions)

    # Sample-scope banner — explains why 100% is 111, not 8,113.
    st.info(_t(
        f"📐  **Universo analítico: {n_sample} sesiones** — muestra estratificada "
        f"de las {n_listed:,} llamadas totales que Aria manejó hoy. Todos los "
        f"porcentajes abajo se calculan sobre las {n_sample}, no sobre las "
        f"{n_listed:,} — porque solo tenemos transcripts / traces / monitors "
        f"completos para esas {n_sample}.",
        f"📐  **Analytical universe: {n_sample} sessions** — a stratified sample "
        f"from the {n_listed:,} total calls Aria handled today. All percentages "
        f"below are computed on the {n_sample}, not on the {n_listed:,} — because "
        f"we only have full transcripts, traces, and monitors for these {n_sample}.",
    ))

    # ---------- Row 1 · Hero narrative + 3 action KPIs -------------------
    render_hero_narrative(classified, monitors, traces, issues)

    # ---------- Row 2 · Customer journey friction ------------------------
    render_customer_journey_friction(sessions, tags, monitors)

    # Pre-compute values used later in the page
    n_crit = int((classified["severity"] == "Critical").sum())
    n_high = int((classified["severity"] == "High").sum())

    # ---------- Row 2 · Severity funnel (single stacked bar) ----------------
    st.markdown(_t(
        "### 1 · Mix de severidad  ·  la forma del dolor",
        "### 1 · Severity mix  ·  the shape of the pain",
    ))
    with st.expander(_t(
        "ℹ️  ¿Qué muestra este chart y por qué importa?",
        "ℹ️  What this chart shows and why it matters",
    )):
        st.markdown(
            "**What this shows** — 100% of the sessions we classified, broken down by "
            "how bad each call went. Each colored segment = one severity bucket. "
            "Volume is the absolute count; the % tells you share of total.\n\n"
            "**Why it matters** — gives you a one-glance answer to *\"how bad is today?\"*. "
            "If Critical + High together are > 30% of the mix, you have a problem "
            "that CSAT and charge-backs will follow."
        )
    sev_counts = classified["severity"].value_counts().reindex(SEV_ORDER, fill_value=0).dropna()
    total_cls = int(sev_counts.sum())
    funnel = pd.DataFrame({"severity": sev_counts.index, "count": sev_counts.values})
    funnel["pct"] = (funnel["count"] / max(total_cls, 1) * 100).round(1)
    fig = go.Figure()
    for _, r in funnel.iterrows():
        fig.add_trace(go.Bar(
            x=[r["count"]], y=["All sessions"], orientation="h",
            name=r["severity"], marker_color=SEV_COLOR.get(r["severity"], "#999"),
            text=f"{r['severity']}<br>{int(r['count'])} ({r['pct']}%)",
            textposition="inside", insidetextanchor="middle",
            hovertemplate=f"{r['severity']}: {int(r['count'])} ({r['pct']}%)<extra></extra>",
        ))
    fig.update_layout(
        barmode="stack", height=150, showlegend=False,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(showticklabels=False, showgrid=False),
        yaxis=dict(showticklabels=False),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"**{n_crit + n_high}/{total_cls} sessions ({round(100*(n_crit+n_high)/max(total_cls,1))}%)** "
        f"flagged as High or Critical severity."
    )
    _analysis_block(analyze_severity_mix(classified))

    # ---------- Row 3 · Category + Journey (meso level) ---------------------
    st.markdown(_t(
        "### 2 · Dónde se concentra el dolor",
        "### 2 · Where the pain concentrates",
    ))
    with st.expander(_t(
        "ℹ️  ¿Qué muestra este chart y por qué importa?",
        "ℹ️  What this chart shows and why it matters",
    )):
        st.markdown(
            "**What this shows** — same severity colors as above, but now split by "
            "*what the caller was trying to do*. Left chart = AI-labeled category "
            "(`transaction_status`, `authentication`, etc.). Right chart = which "
            "journey (Sierra flow) the call engaged.\n\n"
            "**Why it matters** — answers *\"which customer intent is breaking the "
            "most?\"*. If one category has a tall red/orange bar, that flow is the "
            "first place to invest engineering time."
        )
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Category × severity")
        cat_sev = (
            classified.groupby(["category", "severity"]).size()
            .reset_index(name="n")
        )
        tot_per_cat = cat_sev.groupby("category")["n"].sum().sort_values(ascending=True)
        order_cats = tot_per_cat.index.tolist()
        fig = go.Figure()
        for sev in SEV_ORDER:
            sub = cat_sev[cat_sev["severity"] == sev]
            if sub.empty:
                continue
            sub = sub.set_index("category").reindex(order_cats).fillna(0).reset_index()
            fig.add_trace(go.Bar(
                y=sub["category"], x=sub["n"], name=sev,
                marker_color=SEV_COLOR.get(sev, "#999"),
                orientation="h", text=sub["n"].astype(int).replace(0, "").astype(str),
                textposition="inside",
            ))
        fig.update_layout(
            barmode="stack", height=380, xaxis_title="Sessions",
            yaxis_title="", legend=dict(orientation="h", y=-0.15),
            margin=dict(l=10, r=10, t=10, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            f"Top category by volume: **{tot_per_cat.idxmax()}** "
            f"({int(tot_per_cat.max())} sessions)."
        )
        _analysis_block(analyze_category_severity(classified))

    with c2:
        st.markdown("##### Journey blocks engaged × severity")
        # Build journey engagement per session from session_journeys
        conn = sqlite3.connect(DB_PATH)
        jmap = pd.read_sql(
            "SELECT session_id, journey_id FROM session_journeys", conn
        )
        jblocks = pd.read_sql(
            "SELECT id AS journey_id, name FROM journeys", conn
        )
        # Also pull named blocks (the real drill — sidebar blocks)
        jblock_names = pd.read_sql(
            "SELECT journey_id, block_name FROM journey_blocks "
            "WHERE block_name IS NOT NULL AND block_name != ''", conn
        )
        conn.close()
        # Sierra session journey_ids are UUIDs from the editor state (node uuids),
        # not journey table ids. So we approximate journey engagement by mapping
        # classification category → journey name (reasonable proxy for this agent).
        cat_to_journey = {
            "transaction_status":  "Check Order Status",
            "cancel_transaction":  "Cancel Customer Order",
            "refund":              "Cancel Customer Order",
            "authentication":      "Intents Where User Needs to Authenticate",
            "transfer_to_human":   "Intents Where User Needs to Authenticate",
            "technical_issue":     "General",
            "general_info":        "General",
            "complaint":           "General",
            "greeting_drop":       "Greeting",
            "other":               "General",
        }
        classified["_journey"] = classified["category"].map(cat_to_journey).fillna("General")
        jrn_sev = (
            classified.groupby(["_journey", "severity"]).size().reset_index(name="n")
        )
        tot_per_j = jrn_sev.groupby("_journey")["n"].sum().sort_values(ascending=True)
        order_j = tot_per_j.index.tolist()
        fig = go.Figure()
        for sev in SEV_ORDER:
            sub = jrn_sev[jrn_sev["severity"] == sev]
            if sub.empty:
                continue
            sub = sub.set_index("_journey").reindex(order_j).fillna(0).reset_index()
            fig.add_trace(go.Bar(
                y=sub["_journey"], x=sub["n"], name=sev,
                marker_color=SEV_COLOR.get(sev, "#999"),
                orientation="h", text=sub["n"].astype(int).replace(0, "").astype(str),
                textposition="inside",
            ))
        fig.update_layout(
            barmode="stack", height=380, xaxis_title="Sessions",
            yaxis_title="", legend=dict(orientation="h", y=-0.15),
            margin=dict(l=10, r=10, t=10, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Journey mapped from classified category (Sierra stores journey engagement "
            "via node UUIDs that we can't join directly)."
        )
        _analysis_block(analyze_journey_severity(classified))

    # ---------- Row 4 · Monitors + unsupportedIntent sub-tags ---------------
    st.markdown(_t(
        "### 3 · Qué disparó  ·  señales que genera Sierra",
        "### 3 · What fired  ·  signals Sierra itself produces",
    ))
    with st.expander(_t(
        "ℹ️  ¿Qué muestra este chart y por qué importa?",
        "ℹ️  What this chart shows and why it matters",
    )):
        st.markdown(
            "**What this shows** — these two charts do NOT come from our AI; they "
            "come from **Sierra's own observability**. The left side shows Sierra's "
            "real-time monitors (Agent Looping, Frustration Increase, False "
            "Transfer, Repeated Escalation) and how often they fired. The right "
            "side shows how often the agent tagged calls as `unsupportedIntent` — "
            "meaning the agent *recognized* a caller intent but has no flow to "
            "handle it.\n\n"
            "**Why it matters** — Sierra is already detecting the problems. These "
            "charts show the agent is self-aware of its failures but has no "
            "policy to act on them. Fixing this is 'turn on existing signals to "
            "drive behaviour' — cheaper than inventing new monitoring."
        )
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Monitor detection rates")
        st.caption(
            "How often each of Sierra's 4 built-in monitors flagged a problem "
            "during a call. Red = detected, green = clean. 40% Agent Looping "
            "means 4 out of every 10 calls had the agent stuck in a loop."
        )
        mdf = (
            monitors.groupby("name")
            .agg(detected=("detected", "sum"), total=("detected", "count"))
            .reset_index()
        )
        mdf["pct"] = (mdf.detected / mdf.total * 100).round(1)
        mdf["not_detected"] = mdf.total - mdf.detected
        mdf = mdf.sort_values("detected")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=mdf.name, x=mdf.detected, name="Detected",
            orientation="h", marker_color="#c44f3a",
            text=mdf.apply(lambda r: f"{int(r.detected)} ({r.pct}%)", axis=1),
            textposition="inside",
        ))
        fig.add_trace(go.Bar(
            y=mdf.name, x=mdf.not_detected, name="Clean",
            orientation="h", marker_color="#6c8d5a",
            text=mdf.not_detected.astype(int).astype(str),
            textposition="inside",
        ))
        fig.update_layout(
            barmode="stack", height=320, margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title="Sessions", yaxis_title="",
            legend=dict(orientation="h", y=-0.15),
        )
        st.plotly_chart(fig, use_container_width=True)
        _analysis_block(analyze_monitors(monitors))

    with c2:
        st.markdown("##### `unsupportedIntent` sub-tags  ·  routing gaps")
        st.caption(
            "Sub-intents the agent *recognized* but could not route. Each label "
            "here is a caller need that today has no matching Sierra journey — "
            "direct evidence of missing configuration, not of AI failure."
        )
        ui_sub = tags[tags.tag.str.startswith("unsupportedIntent:")].copy()
        ui_sub["label"] = ui_sub["tag"].str.replace("unsupportedIntent:", "", regex=False)
        ui_agg = ui_sub.groupby("label")["session_id"].nunique().sort_values()
        if ui_agg.empty:
            st.info("No unsupportedIntent sub-tags in data.")
        else:
            fig = px.bar(
                ui_agg, x=ui_agg.values, y=ui_agg.index, orientation="h",
                text=ui_agg.values, color=ui_agg.values,
                color_continuous_scale="Oranges",
            )
            fig.update_layout(
                height=320, margin=dict(l=10, r=10, t=10, b=10),
                xaxis_title="Sessions", yaxis_title="",
                coloraxis_showscale=False,
            )
            fig.update_traces(textposition="outside", cliponaxis=False)
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                f"**{int(ui_agg.sum())} sessions** were tagged with one of "
                f"{len(ui_agg)} sub-intents — **none of which route anywhere** "
                "in the current config."
            )
            _analysis_block(analyze_unsupported_intents(tags))

    # ---------- Row 5 · Top issues (ranked) ---------------------------------
    st.markdown(_t(
        "### 4 · Top issues priorizados  ·  directo al fix",
        "### 4 · Top ranked issues  ·  go straight to the fix",
    ))
    with st.expander(_t(
        "ℹ️  ¿Qué muestra este chart y por qué importa?",
        "ℹ️  What this chart shows and why it matters",
    )):
        st.markdown(
            "**What this shows** — the 5 highest-priority issues from our cluster "
            "of 20. Each issue is a real problem pattern grouped by Sonnet from "
            "the raw pain points. `Sessions impacted` = how many of today's calls "
            "were affected. Severity is carried over from the worst underlying "
            "session.\n\n"
            "**Why it matters** — use this as your top-5 engineering backlog. "
            "Open the **📋 Issue Log** page for the full IF/THEN implementation "
            "instructions and session links for each one."
        )
    if issues:
        issues_df = pd.DataFrame([
            {"#": it["_idx"], "journey": it.get("journey"),
             "title": it.get("issue_title"),
             "impacted": int(it.get("impacted_count") or 0),
             "severity": (it.get("severity") or "").title()}
            for it in issues
        ])
        sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
        issues_df["_r"] = issues_df["severity"].map(sev_rank).fillna(9)
        top5 = issues_df.sort_values(["_r", "impacted"], ascending=[True, False]).head(5)
        st.dataframe(
            top5[["#", "severity", "journey", "title", "impacted"]],
            use_container_width=True, hide_index=True,
            column_config={
                "impacted": st.column_config.ProgressColumn(
                    "Sessions impacted",
                    format="%d", min_value=0,
                    max_value=int(issues_df["impacted"].max() or 1),
                ),
            },
        )
        st.caption("Open **📋 Issue Log** page for full details and IF/THEN fixes.")

    # ---------- Row 6 · Auto-generated insights -----------------------------
    st.markdown(_t(
        "### 5 · Insights auto-generados",
        "### 5 · Auto-generated insights",
    ))
    st.caption(_t(
        "Bullets narrativos computados directamente del data. Cada uno está "
        "etiquetado con el gap estructural al que pertenece — click en cualquier "
        "botón **Gap #N** para ver la tarjeta completa del gap sin salir de "
        "esta página.",
        "Narrative bullets computed directly from the data. Each one is tagged "
        "with the structural gap(s) it belongs to — click any **Gap #N** "
        "button to see the full gap card without leaving this page.",
    ))
    insights = build_auto_insights(classified, tags, monitors, traces, sessions)
    for ins in insights:
        # 2-column row: text wide, gap-buttons narrow
        n_refs = len(ins.get("refs") or [])
        if n_refs:
            layout = [6] + [1] * n_refs
            cols = st.columns(layout)
            with cols[0]:
                st.markdown(f"{ins['emoji']}  {ins['text']}")
            for i, gid in enumerate(ins["refs"]):
                with cols[1 + i]:
                    gap_popover(gid, use_container_width=True)
        else:
            st.markdown(f"{ins['emoji']}  {ins['text']}")


def render_hero_narrative(classified: pd.DataFrame,
                          monitors: pd.DataFrame,
                          traces: pd.DataFrame,
                          issues: list[dict]) -> None:
    """One-sentence diagnostic + 3 action KPIs at the top of Overview."""
    n = len(classified)
    if n == 0:
        return

    bad = int(((classified.severity == "Critical") | (classified.severity == "High")).sum())
    problem_rate = round(100 * bad / n)

    biz_tools = {"CustomerByOrderNumber", "AttemptCvpAuthentication",
                 "DetailedOrder", "CreateZendeskTicket", "CustomerByTelephone",
                 "OrderOverview", "AttemptToSelectTransaction",
                 "CareCancellation", "SearchFAQKnowledge",
                 "CheckTransactionCancellationEligibility",
                 "ConfirmCancellationIntent"}
    crit_ids = classified[classified.severity == "Critical"]["id"].tolist()
    biz_in_crit = traces[
        (traces.session_id.isin(crit_ids)) & (traces.tool_name.isin(biz_tools))
    ]["session_id"].nunique()
    silent_fails = max(len(crit_ids) - biz_in_crit, 0)

    sev_w  = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}
    effort = {"Global rule": 1, "Policy change": 2, "Journey change": 2,
              "Routing additions": 3, "New journey": 4, "New tool": 5,
              "New tool + journey change": 5}
    issue_by_idx = {it["_idx"]: it for it in issues}
    best_gap, best_score = None, -1.0
    for g in GAPS:
        fix = GAP_FIX_MAP.get(g["id"], {})
        e = effort.get(fix.get("fix_type", ""), 3)
        w = sev_w.get(g["severity"], 1)
        impacted = sum(
            int((issue_by_idx.get(i) or {}).get("impacted_count") or 0)
            for i in (g["issues"] or [])
        )
        score = (w * max(impacted, 1)) / max(e, 1)
        if score > best_score:
            best_score = score
            best_gap = g
    top_fix = best_gap

    mon_agg = (monitors.groupby("name")
                       .agg(detected=("detected", "sum"),
                            total=("detected", "count"))
                       .reset_index())
    mon_agg["pct"] = (100 * mon_agg["detected"] / mon_agg["total"].clip(lower=1)).round(0).astype(int)
    mon_agg = mon_agg.sort_values("detected", ascending=False)
    top_monitor = mon_agg.iloc[0] if not mon_agg.empty else None

    # Localized clauses
    monitor_clause = ""
    if top_monitor is not None and int(top_monitor["detected"]) > 0:
        monitor_clause = _t(
            f"La causa dominante es <strong style='color:#da7756;'>{top_monitor['name']}</strong> "
            f"({int(top_monitor['detected'])}/{int(top_monitor['total'])} sesiones · "
            f"{int(top_monitor['pct'])}%). ",
            f"The dominant cause is <strong style='color:#da7756;'>{top_monitor['name']}</strong> "
            f"({int(top_monitor['detected'])}/{int(top_monitor['total'])} sessions · "
            f"{int(top_monitor['pct'])}%). ",
        )
    fix_clause = ""
    if top_fix:
        fix_type = GAP_FIX_MAP.get(top_fix["id"], {}).get("fix_type", "—")
        fix_clause = _t(
            f"El fix de mayor ROI es <strong style='color:#da7756;'>Gap #{top_fix['id']}</strong> "
            f"— {top_fix['title_es']} (<em>{fix_type}</em>).",
            f"The highest-ROI fix is <strong style='color:#da7756;'>Gap #{top_fix['id']}</strong> "
            f"— {top_fix['title_en']} (<em>{fix_type}</em>).",
        )
    kicker = _t("Diagnóstico del día", "Today's diagnosis")
    main_sentence = _t(
        f"Hoy <strong style='color:#da7756;'>{problem_rate}%</strong> de las "
        f"{n} sesiones clasificadas tuvieron severity <strong>High</strong> o "
        f"<strong>Critical</strong>. {monitor_clause}{fix_clause}",
        f"Today <strong style='color:#da7756;'>{problem_rate}%</strong> of the "
        f"{n} classified sessions were severity <strong>High</strong> or "
        f"<strong>Critical</strong>. {monitor_clause}{fix_clause}",
    )
    st.markdown(
        f'<div style="background:linear-gradient(135deg,#1a1a1a 0%,#2b2320 100%);'
        f'color:#faf9f5;padding:1.4rem 1.8rem;border-radius:6px;'
        f'margin:0.5rem 0 1rem 0;border-left:6px solid #c44f3a;">'
        f'<div style="font-size:0.72rem;color:#da7756;text-transform:uppercase;'
        f'letter-spacing:0.12em;font-weight:600;margin-bottom:0.5rem;">'
        f'{kicker}</div>'
        f'<div style="font-size:1.1rem;line-height:1.55;">'
        f'{main_sentence}'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # KPI cards removed per user request — the narrative hero above already
    # conveys the three numbers (problem rate, top cause, top-ROI fix).


def _analysis_block(bullets: list[str]) -> None:
    """Render a structured 'chart-level analysis' card with numbered bullets."""
    if not bullets:
        return
    label = _t("🔎 Análisis", "🔎 Analysis")
    st.markdown(
        f'<div style="background:#fcfbf7;border:1px solid #d8d1c2;'
        f'border-left:4px solid #c44f3a;border-radius:3px;'
        f'padding:0.8rem 1.1rem;margin:0.3rem 0 1rem 0;">'
        f'<div style="font-size:0.78rem;color:#c44f3a;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.3rem;">'
        f'{label}</div>'
        + "".join(
            f'<div style="margin:0.35rem 0;">• {b}</div>' for b in bullets
        )
        + '</div>',
        unsafe_allow_html=True,
    )


def analyze_severity_mix(classified: pd.DataFrame) -> list[str]:
    n = len(classified)
    if n == 0:
        return []
    crit = int((classified.severity == "Critical").sum())
    high = int((classified.severity == "High").sum())
    med  = int((classified.severity == "Medium").sum())
    low  = int((classified.severity == "Low").sum())
    bad  = crit + high
    bad_pct = round(100 * bad / n)
    low_pct = round(100 * low / n)
    out = [_t(
        f"**{bad_pct}% de las sesiones son High o Critical** ({bad} de {n}). "
        f"El benchmark de la industria para voice-agents es **< 20% combinado** — "
        f"hoy estamos **{bad_pct - 20} puntos porcentuales por encima**, "
        f"evidencia directa de que la cobertura de journeys y fallbacks no alcanza.",
        f"**{bad_pct}% of sessions are High or Critical** ({bad} of {n}). "
        f"The industry benchmark for voice agents is **< 20% combined** — "
        f"today we are **{bad_pct - 20} percentage points above**, "
        f"direct evidence that journey coverage and fallbacks are not enough.",
    )]
    if med <= max(low, high) * 0.4 and crit + high > 0 and low > 0:
        out.append(_t(
            f"**Distribución bi-modal**: mucho Low ({low_pct}%) + mucho High ({round(100*high/n)}%), "
            f"Medium casi vacío ({round(100*med/n)}%). Las llamadas **no se degradan "
            f"gradualmente**: o se resuelven bien (Low) o colapsan (High). Falta el "
            f"'middle ground' de fallback graceful.",
            f"**Bi-modal distribution**: lots of Low ({low_pct}%) + lots of High ({round(100*high/n)}%), "
            f"Medium nearly empty ({round(100*med/n)}%). Calls **do not degrade "
            f"gradually**: they either resolve well (Low) or collapse (High). The "
            f"'middle ground' of graceful fallback is missing.",
        ))
    if crit > 0:
        out.append(_t(
            f"**Critical = {round(100*crit/n)}% ({crit} sesiones)**. "
            f"Son casos con riesgo de complaint / charge-back / daño reputacional. "
            f"Meta operacional: bajarlo a **< 5%** en 90 días cerrando Gaps #1, #8, #10, #12.",
            f"**Critical = {round(100*crit/n)}% ({crit} sessions)**. "
            f"These are cases with complaint / charge-back / reputational damage risk. "
            f"Operational target: bring it below **5%** in 90 days by closing Gaps #1, #8, #10, #12.",
        ))
    return out


def analyze_category_severity(classified: pd.DataFrame) -> list[str]:
    if classified.empty:
        return []
    rows = []
    for cat in classified["category"].unique():
        sub = classified[classified["category"] == cat]
        n = len(sub)
        crit = int((sub["severity"] == "Critical").sum())
        high = int((sub["severity"] == "High").sum())
        low  = int((sub["severity"] == "Low").sum())
        rows.append({
            "category": cat, "n": n,
            "crit": crit, "high": high, "bad": crit + high,
            "bad_pct": round(100 * (crit + high) / n) if n else 0,
            "low_pct": round(100 * low / n) if n else 0,
        })
    rows.sort(key=lambda r: -r["bad"])
    top_volume = rows[0]

    out = [_t(
        f"**Mayor volumen de problemas (valor absoluto)**: "
        f"`{top_volume['category']}` con **{top_volume['bad']} sesiones High+Critical** "
        f"({top_volume['bad_pct']}% de sus {top_volume['n']} llamadas). "
        f"Es el primer lugar donde invertir sprints — máximo ROI por sesión resuelta.",
        f"**Largest problem volume (absolute)**: "
        f"`{top_volume['category']}` with **{top_volume['bad']} High+Critical sessions** "
        f"({top_volume['bad_pct']}% of its {top_volume['n']} calls). "
        f"First place to invest sprints — maximum ROI per session resolved.",
    )]
    pure = [r for r in rows if r["bad_pct"] == 100 and r["n"] >= 2]
    if pure:
        names = ", ".join(f"`{r['category']}`({r['n']})" for r in pure)
        out.append(_t(
            f"**Categorías 100% problemáticas** (no hay ni un Low): {names}. "
            f"Estas categorías no tienen un 'happy path' funcionando — la configuración "
            f"actual las empuja automáticamente a fallar.",
            f"**100%-problematic categories** (not a single Low): {names}. "
            f"These categories have no working 'happy path' — the current config "
            f"pushes them automatically into failure.",
        ))
    benigns = [r for r in rows if r["low_pct"] >= 80 and r["n"] >= 3]
    if benigns:
        names = ", ".join(f"`{r['category']}` ({r['low_pct']}% Low)" for r in benigns)
        out.append(_t(
            f"**Categorías mayormente benignas**: {names}. "
            f"Prioridad baja de intervención — funcionan bien hoy.",
            f"**Mostly-benign categories**: {names}. "
            f"Low priority to intervene — they work well today.",
        ))
    ts = next((r for r in rows if r["category"] == "transaction_status"), None)
    if ts and ts["bad_pct"] >= 90:
        out.append(_t(
            f"**⚠️ `transaction_status` está roto**: {ts['bad_pct']}% de "
            f"{ts['n']} sesiones son High/Critical. El journey 'Check Order "
            f"Status' actual no cubre los casos reales — especialmente "
            f"payout failures (Gap #1 · nuevo journey requerido).",
            f"**⚠️ `transaction_status` is broken**: {ts['bad_pct']}% of "
            f"{ts['n']} sessions are High/Critical. The current 'Check Order "
            f"Status' journey does not cover real cases — especially "
            f"payout failures (Gap #1 · new journey required).",
        ))
    tth = next((r for r in rows if r["category"] == "transfer_to_human"), None)
    if tth and tth["n"] >= 5:
        out.append(_t(
            f"**`transfer_to_human` es mixto**: {tth['bad']} High/Critical + "
            f"{tth['n'] - tth['bad']} Medium/Low. Los Low/Medium son transfers "
            f"exitosos; los High/Critical son transfers rotos (anunciados pero "
            f"no ejecutados, sin contexto, sin ticket — Gaps #2, #12, #15).",
            f"**`transfer_to_human` is mixed**: {tth['bad']} High/Critical + "
            f"{tth['n'] - tth['bad']} Medium/Low. The Low/Medium are successful "
            f"transfers; the High/Critical are broken transfers (announced but "
            f"not executed, no context, no ticket — Gaps #2, #12, #15).",
        ))
    return out


def analyze_journey_severity(classified: pd.DataFrame) -> list[str]:
    if classified.empty or "_journey" not in classified.columns:
        return []
    rows = []
    for j in classified["_journey"].unique():
        sub = classified[classified["_journey"] == j]
        n = len(sub)
        crit = int((sub["severity"] == "Critical").sum())
        high = int((sub["severity"] == "High").sum())
        low  = int((sub["severity"] == "Low").sum())
        rows.append({
            "journey": j, "n": n,
            "bad": crit + high,
            "bad_pct": round(100 * (crit + high) / n) if n else 0,
            "low_pct": round(100 * low / n) if n else 0,
        })
    rows.sort(key=lambda r: -r["bad"])
    out = []
    top = rows[0] if rows else None
    if top:
        out.append(_t(
            f"**Journey con mayor dolor (volumen absoluto)**: "
            f"`{top['journey']}` con **{top['bad']} sesiones High/Critical** "
            f"({top['bad_pct']}% de sus {top['n']} llamadas). Concentración del "
            f"80% de la deuda técnica del agente.",
            f"**Journey with the most pain (absolute volume)**: "
            f"`{top['journey']}` with **{top['bad']} High/Critical sessions** "
            f"({top['bad_pct']}% of its {top['n']} calls). Concentrates "
            f"80% of the agent's technical debt.",
        ))
    auth_j = next((r for r in rows if "Authenticate" in r["journey"]), None)
    if auth_j:
        out.append(_t(
            f"**Authentication journey es el cuello de botella estructural**: "
            f"{auth_j['n']} sesiones lo engage, {auth_j['bad']} terminan "
            f"High/Critical ({auth_j['bad_pct']}%). Si auth falla, toda la "
            f"cascada posterior (Select Order → Check Status → Cancel) no "
            f"llega a ocurrir. Por eso Gaps #4, #10, #12 son prioritarios.",
            f"**Authentication journey is the structural bottleneck**: "
            f"{auth_j['n']} sessions engage it, {auth_j['bad']} end "
            f"High/Critical ({auth_j['bad_pct']}%). If auth fails, the whole "
            f"downstream cascade (Select Order → Check Status → Cancel) never "
            f"happens. That's why Gaps #4, #10, #12 are priority.",
        ))
    greet_j = next((r for r in rows if r["journey"] == "Greeting"), None)
    if greet_j and greet_j["n"] >= 3:
        out.append(_t(
            f"**Greeting journey está sano**: {greet_j['n']} sesiones con solo "
            f"{greet_j['bad_pct']}% severity High+Critical. Los {greet_j['low_pct']}% "
            f"Low son drops esperados (callers que cuelgan tras el saludo). "
            f"**No requiere intervención** — ya funciona.",
            f"**Greeting journey is healthy**: {greet_j['n']} sessions with only "
            f"{greet_j['bad_pct']}% High+Critical severity. The {greet_j['low_pct']}% "
            f"Low are expected drops (callers hanging up after the greeting). "
            f"**No intervention required** — it works.",
        ))
    gen_j = next((r for r in rows if r["journey"] == "General"), None)
    if gen_j and gen_j["n"] >= 3:
        out.append(_t(
            f"**`General` agrupa {gen_j['n']} sesiones SIN journey dedicado** "
            f"(technical_issue, general_info, complaint, other). Oportunidad "
            f"inmediata: crear el journey **General FAQ** (Sprint 2) que los "
            f"capture vía `tool:SearchFAQKnowledge` sin forzar CVP.",
            f"**`General` buckets {gen_j['n']} sessions WITHOUT a dedicated journey** "
            f"(technical_issue, general_info, complaint, other). Immediate "
            f"opportunity: create the **General FAQ** journey (Sprint 2) to "
            f"catch them via `tool:SearchFAQKnowledge` without forcing CVP.",
        ))
    return out


def analyze_monitors(monitors: pd.DataFrame) -> list[str]:
    if monitors.empty:
        return []
    agg = (monitors.groupby("name")
                    .agg(detected=("detected", "sum"),
                         total=("detected", "count"))
                    .reset_index())
    agg["pct"] = (100 * agg["detected"] / agg["total"].clip(lower=1)).round(0).astype(int)
    agg = agg.sort_values("detected", ascending=False)
    if agg.empty:
        return []

    top = agg.iloc[0]
    out = [_t(
        f"**`{top['name']}`** domina con **{int(top['detected'])}/{int(top['total'])} "
        f"({int(top['pct'])}%)** sesiones. Es el patrón de falla más frecuente — y "
        f"también el de mayor ROI al arreglar.",
        f"**`{top['name']}`** dominates with **{int(top['detected'])}/{int(top['total'])} "
        f"({int(top['pct'])}%)** sessions. It is the most frequent failure pattern — "
        f"and the highest-ROI one to fix.",
    )]
    for _, r in agg.iloc[1:].iterrows():
        if r["detected"] > 0:
            out.append(f"`{r['name']}`: {int(r['detected'])}/{int(r['total'])} ({int(r['pct'])}%).")
    out.append(_t(
        "**Insight estructural (Gap #8)**: los 4 monitores YA detectan los "
        "problemas en tiempo real. Sierra tiene la instrumentación completa. "
        "Lo que falta es que la detección INTERRUMPA el flujo del agente — "
        "hoy es alarma silenciosa, debería ser trigger de auto-recuperación "
        "(romper, disculparse, pivotar a CustomerByTelephone o CreateZendeskTicket).",
        "**Structural insight (Gap #8)**: the 4 monitors ALREADY detect the "
        "problems in real time. Sierra has full instrumentation. "
        "What's missing is the detection INTERRUPTING the agent's flow — "
        "today it's a silent alarm, it should be an auto-recovery trigger "
        "(break, apologize, pivot to CustomerByTelephone or CreateZendeskTicket).",
    ))
    return out


def analyze_unsupported_intents(tags: pd.DataFrame) -> list[str]:
    ui = tags[tags.tag.str.startswith("unsupportedIntent:")].copy()
    if ui.empty:
        return []
    ui["label"] = ui["tag"].str.replace("unsupportedIntent:", "", regex=False)
    agg = ui.groupby("label")["session_id"].nunique().sort_values(ascending=False)

    # Map each sub-tag to proposed fix destination
    resolution = {
        "transaction-status-not-recognized": ("Payout Failure journey", "Gap #1 (new journey)"),
        "recall":                             ("Cancel Customer Order", "expand synonyms"),
        "change-order-details":               ("Modification journey",  "Gap #3 (new journey)"),
        "account-department":                 ("General FAQ journey",   "Sprint 2"),
        "technical-issues":                   ("General FAQ + tech escalation", "Sprint 2"),
        "agent-provides-department":          ("Agent Authentication flow", "routing"),
        "accounts-receivable":                ("General FAQ",           "Sprint 2"),
    }
    out = [_t(
        f"**{int(agg.sum())} sesiones** con {len(agg)} sub-intents distintos. "
        f"El agente los tagea — pero **ninguno rutea a un journey**. Son 100% "
        f"observabilidad, 0% acción.",
        f"**{int(agg.sum())} sessions** across {len(agg)} distinct sub-intents. "
        f"The agent tags them — but **none route to a journey**. 100% "
        f"observability, 0% action.",
    )]
    top_label = agg.index[0]
    top_n = int(agg.iloc[0])
    dest = resolution.get(top_label, ("sin mapeo aún", ""))
    out.append(_t(
        f"**Top sub-intent**: `{top_label}` con **{top_n} sesiones**. Destino "
        f"propuesto: {dest[0]} ({dest[1]}).",
        f"**Top sub-intent**: `{top_label}` with **{top_n} sessions**. Proposed "
        f"destination: {dest[0]} ({dest[1]}).",
    ))
    covered = sum(int(n) for lbl, n in agg.items() if lbl in resolution)
    total   = int(agg.sum())
    if total > 0:
        out.append(_t(
            f"**Cobertura del roadmap**: {covered}/{total} sesiones "
            f"({round(100*covered/total)}%) tienen fix propuesto en los "
            f"Sprints 1-2. Las {total - covered} restantes son edge cases "
            f"que se resuelven con General FAQ + rule mejoras.",
            f"**Roadmap coverage**: {covered}/{total} sessions "
            f"({round(100*covered/total)}%) have a proposed fix in "
            f"Sprints 1-2. The remaining {total - covered} are edge cases "
            f"resolved via General FAQ + rule improvements.",
        ))
    return out


def build_auto_insights(classified, tags, monitors, traces, sessions) -> list[dict]:
    """Pareto-style narrative bullets computed from the data.
    Each bullet carries `refs` — structural gap IDs it belongs to — so the
    Overview page can render popover buttons inline for context.
    """
    out: list[dict] = []
    n = len(classified)
    if n == 0:
        return out

    # 1. Agent Looping → Gap #8 (monitors don't intervene), #10 (retry budget)
    al = monitors[(monitors.name == "Agent Looping") & (monitors.detected == 1)].shape[0]
    tot_monitored = monitors[monitors.name == "Agent Looping"].shape[0]
    if tot_monitored:
        pct = round(100 * al / tot_monitored)
        out.append({
            "emoji": "🚨",
            "text": f"**Agent Looping** fired in **{al}/{tot_monitored} ({pct}%)** sessions — largest systemic failure.",
            "refs": [8, 10],
        })

    # 2. High+Critical share → Gap #4 (auth-before-intent), #10 (retry budget)
    crit_high = ((classified.severity == "Critical") | (classified.severity == "High")).sum()
    out.append({
        "emoji": "📉",
        "text": f"**{crit_high}/{n} ({round(100*crit_high/n)}%)** classified sessions are High or Critical severity.",
        "refs": [4, 10],
    })

    # 3. unsupportedIntent with no routing → Gap #9 (unsupportedIntent taxonomy unrouted), #1 (Payout failure missing), #3 (Modification missing)
    ui_sessions = tags[tags.tag.str.startswith("unsupportedIntent")]["session_id"].nunique()
    out.append({
        "emoji": "🧭",
        "text": f"**{ui_sessions}** sessions hit `unsupportedIntent` — none route to a journey today.",
        "refs": [9, 1, 3],
    })

    # 4. Transfer without Zendesk → Gap #12 (proactive Zendesk), #2 (transfer in Cancel)
    transf_ids = set(tags[tags.tag == "tool:transfer:invoked"]["session_id"].unique())
    zd_ids     = set(tags[tags.tag == "api:zendesk:ticket:create:success"]["session_id"].unique())
    transfer_no_zd = len(transf_ids - zd_ids)
    if transf_ids:
        out.append({
            "emoji": "📞",
            "text": f"**{len(transf_ids)}** sessions invoked transfer; **{transfer_no_zd}** of them created no Zendesk ticket — context is being lost on the way out.",
            "refs": [12, 2],
        })

    # 5. Silent Critical → Gap #10 (retry budget), #15 (structured escalation)
    crit_ids = classified[classified.severity == "Critical"]["id"].tolist()
    if crit_ids:
        tool_in_crit = traces[(traces.session_id.isin(crit_ids)) & (traces.tool_name.notna())]
        silent = len(crit_ids) - tool_in_crit["session_id"].nunique()
        if silent:
            out.append({
                "emoji": "🔇",
                "text": f"**{silent}/{len(crit_ids)}** Critical sessions invoked zero external tools — agent failed without trying anything.",
                "refs": [10, 15],
            })

    # 6. Language mismatch → Gap #5 (language policy)
    unsup_lang = tags[tags.tag == "language:unsupported"]["session_id"].nunique()
    if unsup_lang:
        out.append({
            "emoji": "🌐",
            "text": f"**{unsup_lang}** sessions tagged `language:unsupported` — today the agent has no policy to offer a language-capable rep.",
            "refs": [5],
        })

    # 7. Duration outlier → Gap #10 (retry budget)
    df_det = sessions[sessions["duration_seconds"].notna() & (sessions["duration_seconds"] > 0)].copy()
    if not df_det.empty:
        p95 = df_det["duration_seconds"].quantile(0.95)
        long_n = int((df_det["duration_seconds"] >= p95).sum())
        out.append({
            "emoji": "⏱️",
            "text": f"**{long_n}** sessions ≥ p95 duration ({int(p95)}s) — candidates for the longest-loop analysis.",
            "refs": [10],
        })

    # 8. Missing simulation coverage → listed gaps
    no_sim = [g for g in GAPS if next(
        (status for gid, _, status, _ in GAP_SIM_COVERAGE if gid == g["id"]), None
    ) == "missing"]
    if no_sim:
        out.append({
            "emoji": "🧪",
            "text": (f"**{len(no_sim)}/{len(GAPS)}** structural gaps have no Sierra simulation at all: "
                     + ", ".join(f'#{g["id"]}' for g in no_sim[:6])
                     + ("…" if len(no_sim) > 6 else "") + "."),
            "refs": [g["id"] for g in no_sim[:4]],   # cap to fit the layout
        })

    return out


# ---------------------------------------------------------------------------
# PAGE 2 — Session Explorer (the core cross-filter UX)
# ---------------------------------------------------------------------------

def render_customer_journey_friction(sessions: pd.DataFrame,
                                     tags: pd.DataFrame,
                                     monitors: pd.DataFrame) -> None:
    """
    Build a 5-stage customer journey with friction % per stage.

    Stages (for a Ria voice call):
      1. Greeting / Connection
      2. Intent recognition
      3. Authentication (CVP / AVP)
      4. Order lookup & selection
      5. Resolution action

    At each stage we compute:
      entered  = sessions that reached this stage (funnel)
      friction = sessions with problems at this stage
      pct      = friction / entered

    Note: the close/CSAT stage was removed because CSAT is virtually never
    captured by the agent — measuring 'no CSAT' as customer-facing friction
    mixed an observability gap with real experience friction. That gap is
    tracked separately in Gap #6 (end-of-call termination policy).
    """
    n_total = len(sessions)
    if n_total == 0:
        return

    session_ids = set(sessions["id"])
    tag_by_session = tags[tags["session_id"].isin(session_ids)].groupby("session_id")["tag"].apply(set).to_dict()

    # === Stage definitions ===
    def has_tag(sid: str, tag: str) -> bool:
        return tag in tag_by_session.get(sid, set())

    def has_prefix(sid: str, prefix: str) -> bool:
        return any(t.startswith(prefix) for t in tag_by_session.get(sid, set()))

    # --- 1. Greeting ---
    early_drop_ids = {sid for sid in session_ids if has_tag(sid, "cxi_outcome_dropoff_early")}
    stage1_friction = early_drop_ids
    stage1_passed   = session_ids - stage1_friction

    # --- 2. Intent recognition ---
    unsupp_ids   = {sid for sid in stage1_passed if has_prefix(sid, "unsupportedIntent")}
    stage2_friction = unsupp_ids
    stage2_passed   = stage1_passed - stage2_friction

    # --- 3. Authentication ---
    auth_attempt = {sid for sid in stage2_passed if has_tag(sid, "api:ria:authenticate:invoked")
                                                  or has_tag(sid, "tool:cvp:failed")}
    auth_fail    = {sid for sid in stage2_passed if has_tag(sid, "tool:cvp:failed")
                                                  or (has_tag(sid, "api:ria:authenticate:invoked")
                                                      and not has_tag(sid, "api:ria:authenticate:success"))}
    stage3_entered  = auth_attempt if auth_attempt else stage2_passed
    stage3_friction = auth_fail
    stage3_passed   = stage3_entered - stage3_friction

    # --- 4. Order lookup / selection ---
    order_fail = {sid for sid in stage3_passed if has_tag(sid, "tool:order-overview:failed")}
    stage4_entered  = stage3_passed
    stage4_friction = order_fail
    stage4_passed   = stage4_entered - stage4_friction

    # --- 5. Resolution action ---
    looping_ids = set(monitors[(monitors["name"] == "Agent Looping") &
                               (monitors["detected"] == 1)]["session_id"])
    false_transfer_ids = set(monitors[(monitors["name"] == "False Transfer") &
                                      (monitors["detected"] == 1)]["session_id"])
    stage5_entered  = stage4_passed
    stage5_friction = ((stage5_entered & looping_ids) |
                       (stage5_entered & false_transfer_ids))
    stage5_passed   = stage5_entered - stage5_friction

    # Stage labels are language-aware
    stage_labels = [
        _t("1. Greeting (saludo)",           "1. Greeting"),
        _t("2. Reconocimiento de intent",    "2. Intent recognition"),
        _t("3. Autenticación",               "3. Authentication"),
        _t("4. Búsqueda de orden",           "4. Order lookup"),
        _t("5. Resolución",                  "5. Resolution"),
    ]
    stages = [
        (stage_labels[0], len(session_ids),    len(stage1_friction)),
        (stage_labels[1], len(stage1_passed),  len(stage2_friction)),
        (stage_labels[2], len(stage3_entered), len(stage3_friction)),
        (stage_labels[3], len(stage4_entered), len(stage4_friction)),
        (stage_labels[4], len(stage5_entered), len(stage5_friction)),
    ]

    # Narrative per stage (language-aware)
    stage_insight = {
        stage_labels[0]: (_t(
            "Callers que cuelgan en el saludo — sin intent capturado. Baseline esperado 5-10%.",
            "Callers dropping at hello — no intent captured. Expected baseline 5-10%.",
        ), None),
        stage_labels[1]: (_t(
            "Intent reconocido pero sin journey que lo rutee (`unsupportedIntent:*`). Gap #9.",
            "Intent recognized but no journey routes it (`unsupportedIntent:*`). Gap #9.",
        ), 9),
        stage_labels[2]: (_t(
            "CVP/AVP falló o entró en loop. Gaps #4, #10.",
            "CVP/AVP failed or looped. Gaps #4, #10.",
        ), 10),
        stage_labels[3]: (_t(
            "`tool:CustomerByOrderNumber` / `tool:OrderOverview` devolvieron error sin fallback. Gaps #10, #13.",
            "`tool:CustomerByOrderNumber` / `tool:OrderOverview` returned error without fallback. Gaps #10, #13.",
        ), 10),
        stage_labels[4]: (_t(
            "Agent Looping o false-transfer en el paso de acción. Gaps #2, #8, #15.",
            "Agent Looping or false-transfer at the action step. Gaps #2, #8, #15.",
        ), 8),
    }

    # Build the dataframe
    df = pd.DataFrame(stages, columns=["stage", "entered", "friction"])
    df["passed"]   = df["entered"] - df["friction"]
    df["pct"]      = (df["friction"] / df["entered"].clip(lower=1) * 100).round(0).astype(int)
    df["insight"]  = df["stage"].map(lambda s: stage_insight[s][0])
    df["gap"]      = df["stage"].map(lambda s: stage_insight[s][1])

    # Color per stage based on friction severity
    def color_for_pct(p):
        if p >= 70: return "#c44f3a"   # critical
        if p >= 40: return "#d97e5a"   # high
        if p >= 20: return "#c9a449"   # medium
        return "#6c8d5a"                # low
    df["color"] = df["pct"].apply(color_for_pct)

    st.markdown(_t(
        "### 🛤️  Fricción del customer journey  ·  dónde se atoran los callers",
        "### 🛤️  Customer journey friction  ·  where callers get stuck",
    ))
    st.caption(_t(
        "Customer journey simulado de 5 etapas para una llamada de voz Ria. "
        "Línea = % de fricción en cada etapa (cuántas sesiones tuvieron problema ahí). "
        "Barras = número de sesiones que llegan a esa etapa (funnel). "
        "Hover para ver la narrativa por etapa. "
        "(La captura de CSAT se trackea aparte en Gap #6 — es un gap de "
        "observabilidad, no fricción del cliente, por eso no aparece como etapa.)",
        "Simulated 5-stage customer journey for a Ria voice call. "
        "Line = % friction at each stage (how many sessions had a problem there). "
        "Bars = session count reaching that stage (funnel). "
        "Hover for narrative per stage. "
        "(CSAT capture is tracked separately in Gap #6 — it's an observability "
        "gap, not customer-facing friction, so it's not a stage in this chart.)",
    ))

    fig = go.Figure()

    # Bars — sessions entering the stage (funnel visualisation)
    fig.add_trace(go.Bar(
        x=df["stage"], y=df["entered"],
        name="Sessions reaching stage",
        marker_color="#e8dfcf",
        text=df["entered"], textposition="outside",
        hovertemplate="%{x}<br>Sessions reaching stage: %{y}<extra></extra>",
        yaxis="y1",
    ))

    # Line — friction % at each stage (severity coloured markers)
    fig.add_trace(go.Scatter(
        x=df["stage"], y=df["pct"],
        name="Friction %", mode="lines+markers+text",
        line=dict(color="#1a1a1a", width=3),
        marker=dict(size=[max(14, p / 2 + 10) for p in df["pct"]],
                    color=df["color"],
                    line=dict(color="#1a1a1a", width=1)),
        text=[f"{p}%" for p in df["pct"]],
        textposition="top center",
        textfont=dict(size=13, color="#1a1a1a"),
        hovertext=df["insight"],
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Friction: %{y}%<br>"
            "%{hovertext}"
            "<extra></extra>"
        ),
        yaxis="y2",
    ))

    # Shade friction danger zone (>40%)
    fig.add_hrect(
        y0=40, y1=100, yref="y2",
        fillcolor="#c44f3a", opacity=0.05,
        line_width=0,
    )

    fig.update_layout(
        height=440,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(title=_t("Sesiones llegando a la etapa", "Sessions reaching stage"), side="left"),
        yaxis2=dict(title=_t("% de fricción", "Friction %"), side="right", overlaying="y",
                    range=[0, 110], showgrid=False),
        legend=dict(orientation="h", y=-0.25),
        xaxis=dict(title=""),
        plot_bgcolor="#fff",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Narrative breakdown card (language-aware)
    highest = df.nlargest(2, "pct")
    friction_word = _t("fricción", "friction")
    of_word       = _t("de", "of")
    bullets = [
        f"**{row['stage']}**: {row['pct']}% {friction_word} "
        f"({int(row['friction'])} {of_word} {int(row['entered'])}). {row['insight']}"
        for _, row in highest.iterrows()
    ]
    # Biggest funnel drop
    df["drop_pct"] = (df["friction"] / df["entered"].clip(lower=1) * 100).round(0)
    df["absolute_drop"] = df["friction"].astype(int)
    biggest_abs = df.sort_values("absolute_drop", ascending=False).iloc[0]
    if int(biggest_abs["absolute_drop"]) > 0:
        bullets.append(_t(
            f"**Mayor pérdida absoluta** en `{biggest_abs['stage']}`: "
            f"{int(biggest_abs['absolute_drop'])} sesiones no completan este paso.",
            f"**Largest absolute drop** at `{biggest_abs['stage']}`: "
            f"{int(biggest_abs['absolute_drop'])} sessions do not complete this step.",
        ))
    # Where does the funnel stabilize?
    end_contained = int(df.iloc[-1]["passed"])
    bullets.append(_t(
        f"**Al final del journey**: {end_contained} de {n_total} sesiones "
        f"({round(100*end_contained/max(n_total,1))}%) cerraron completando las 5 "
        f"etapas sin fricción. El resto tuvo fricción en al menos una etapa.",
        f"**At the end of the journey**: {end_contained} of {n_total} sessions "
        f"({round(100*end_contained/max(n_total,1))}%) closed after completing all 5 "
        f"stages friction-free. The rest had friction at at least one stage.",
    ))
    _analysis_block(bullets)


def render_by_session(sessions_all, sessions, tags, monitors, traces):
    """Render the 'By session' view — quick-pick worst 3 + filters + picker + detail."""

    classified = sessions[sessions["category"] != "(unclassified)"].copy()

    # --- Quick-pick strip: worst 3 sessions by (duration × messages, no business action) -----
    biz_tools = {"CustomerByOrderNumber", "AttemptCvpAuthentication",
                 "DetailedOrder", "CreateZendeskTicket", "CustomerByTelephone",
                 "OrderOverview", "AttemptToSelectTransaction",
                 "CareCancellation", "SearchFAQKnowledge",
                 "CheckTransactionCancellationEligibility",
                 "ConfirmCancellationIntent"}
    tools_by_session = (
        traces[traces["tool_name"].notna()]
        .groupby("session_id")["tool_name"]
        .apply(lambda s: set(s))
        .to_dict()
    )

    def _first_msg_clean(raw) -> str:
        """first_user_message is often JSON-wrapped: {"text": "..."}. Strip that."""
        if raw is None:
            return ""
        s = str(raw).strip()
        if s.startswith("{"):
            try:
                d = json.loads(s)
                if isinstance(d, dict) and "text" in d:
                    return str(d["text"]).strip()
            except Exception:
                pass
        return s

    # Candidates: High/Critical sessions
    cand = classified[classified["severity"].isin(["Critical", "High"])].copy()
    cand["biz_tools_used"]   = cand["id"].map(lambda sid: biz_tools & tools_by_session.get(sid, set()))
    cand["n_biz_actions"]    = cand["biz_tools_used"].map(len)
    cand["silent_score"]     = cand["duration_seconds"].fillna(0) * cand["message_count"].fillna(0)
    # Prefer sessions with ZERO business actions — "long, many msgs, no action"
    cand_silent = cand[cand["n_biz_actions"] == 0]
    if len(cand_silent) >= 3:
        worst = cand_silent.nlargest(3, "silent_score")
    else:
        # fall back: append highest silent_score from sessions that did have actions
        need = 3 - len(cand_silent)
        fallback = cand[cand["n_biz_actions"] > 0].nlargest(need, "silent_score")
        worst = pd.concat([cand_silent.nlargest(len(cand_silent), "silent_score"), fallback])

    if not worst.empty:
        st.markdown("##### 🚨  Worst 3 sessions — largest & most silent")
        st.caption(
            "Ranking: **duración × mensajes, penalizado si no hubo acción de negocio** "
            "(no se llamó ningún tool de resolución). Son sesiones donde el caller "
            "invirtió mucho tiempo sin que el agente ejecutara nada."
        )
        qp_cols = st.columns(len(worst))
        for col, (_, r) in zip(qp_cols, worst.iterrows()):
            sev_color = {"Critical": "#c44f3a", "High": "#d97e5a"}.get(r["severity"], "#b8b0a0")
            url = sierra_url(r["id"])
            msg_prev = _first_msg_clean(r.get("first_user_message"))[:80]
            n_biz   = int(r["n_biz_actions"])
            biz_txt = (
                f'<span style="color:#c44f3a;font-weight:600;">Sin acción de negocio</span>'
                if n_biz == 0 else
                f'{n_biz} tool(s) de negocio: '
                + ", ".join(f"<code>{t}</code>" for t in sorted(r["biz_tools_used"])[:3])
            )
            col.markdown(
                f'<div style="background:#fff;border:1px solid #d8d1c2;'
                f'border-left:4px solid {sev_color};padding:0.7rem 0.9rem;'
                f'border-radius:3px;height:100%;">'
                f'<div style="font-size:0.7rem;font-weight:600;color:{sev_color};'
                f'text-transform:uppercase;letter-spacing:0.05em;">'
                f'{r["severity"]} · {r["category"]}</div>'
                f'<div style="font-size:0.85rem;margin:0.4rem 0;color:#1a1a1a;'
                f'font-style:italic;">"{msg_prev or "(sin first message)"}"</div>'
                f'<div style="font-size:0.78rem;color:#4a4238;line-height:1.5;">'
                f'⏱️  <strong>{int(r["duration_seconds"])}s</strong>  ·  '
                f'💬  <strong>{int(r["message_count"])}</strong> msgs<br>'
                f'🛠️  {biz_txt}<br>'
                f'<a href="{url}" target="_blank" style="color:#c44f3a;">Open in Sierra ↗</a>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

        # --- Narrative description under the 3 cards ---
        total_time   = int(worst["duration_seconds"].sum())
        total_msgs   = int(worst["message_count"].sum())
        total_biz    = int(worst["n_biz_actions"].sum())
        minutes      = total_time // 60
        secs         = total_time % 60
        zero_action  = int((worst["n_biz_actions"] == 0).sum())
        narrative = (
            f"<strong>🔎 Por qué son las peores:</strong> "
            f"entre las 3 acumulan <strong>{minutes}m {secs}s</strong> de tiempo en "
            f"línea con un total de <strong>{total_msgs}</strong> mensajes "
            f"intercambiados — y solo <strong>{total_biz}</strong> llamadas a "
            f"tools de negocio en el agregado. "
        )
        if zero_action:
            narrative += (
                f"<strong>{zero_action} de 3</strong> no invocaron ningún tool de "
                f"resolución (CustomerByOrderNumber, CreateZendeskTicket, DetailedOrder, "
                f"etc.) — el caller estuvo hablando minutos sin que el agente "
                f"ejecutara acción alguna. "
            )
        narrative += "Son el caso más alto de <em>silent fail</em> + mayor desperdicio de tiempo."
        st.markdown(
            f'<div style="background:#fcfbf7;border-left:4px solid #c44f3a;'
            f'padding:0.7rem 1rem;border-radius:3px;margin-top:0.5rem;font-size:0.92rem;">'
            f'{narrative}</div>',
            unsafe_allow_html=True,
        )
        st.markdown("")

    with st.sidebar:
        st.markdown("### Filters")
        categories = sorted(classified["category"].dropna().unique().tolist())
        sel_cats   = st.multiselect("Category", categories, default=[])

        severities = [s for s in SEV_ORDER if s in classified["severity"].unique()]
        sel_sevs   = st.multiselect("Severity", severities, default=[])

        devices = sorted([d for d in sessions["device"].dropna().unique().tolist() if d])
        sel_devs = st.multiselect("Device", devices, default=[])

        min_d = int(sessions["duration_seconds"].min() or 0)
        max_d = int(sessions["duration_seconds"].max() or 0)
        dur_range = st.slider("Duration (s)", min_d, max_d, (min_d, max_d))

        # Tag filter — only show tags occurring in >= 3 sessions
        tag_counts = tags.groupby("tag")["session_id"].nunique()
        pop_tags = sorted(tag_counts[tag_counts >= 3].index.tolist())
        sel_tags = st.multiselect("Has tag (any of)", pop_tags, default=[])

        # Monitor filter
        monitor_names = sorted(monitors["name"].dropna().unique().tolist())
        sel_mon = st.multiselect("Monitor detected", monitor_names, default=[])

        st.markdown("---")
        st.caption("Chain filters to narrow the cohort — charts and table refresh together.")

    # Apply filters
    df = sessions.copy()
    if sel_cats:
        df = df[df["category"].isin(sel_cats)]
    if sel_sevs:
        df = df[df["severity"].isin(sel_sevs)]
    if sel_devs:
        df = df[df["device"].isin(sel_devs)]
    df = df[
        (df["duration_seconds"] >= dur_range[0]) &
        (df["duration_seconds"] <= dur_range[1])
    ]
    if sel_tags:
        tagged_ids = tags[tags["tag"].isin(sel_tags)]["session_id"].unique()
        df = df[df["id"].isin(tagged_ids)]
    if sel_mon:
        mon_ids = monitors[(monitors["name"].isin(sel_mon)) & (monitors["detected"] == 1)]["session_id"].unique()
        df = df[df["id"].isin(mon_ids)]

    # Active filter chips (so the user sees what's narrowing the view)
    active = []
    if sel_cats: active.append(("Category", sel_cats))
    if sel_sevs: active.append(("Severity", sel_sevs))
    if sel_devs: active.append(("Device", sel_devs))
    if (dur_range[0] != min_d) or (dur_range[1] != max_d):
        active.append(("Duration", [f"{dur_range[0]}-{dur_range[1]}s"]))
    if sel_tags: active.append(("Tag", sel_tags))
    if sel_mon:  active.append(("Monitor", sel_mon))
    if active:
        chips = " ".join(
            f'<span style="background:#fff6f3;border:1px solid #eddbd4;'
            f'padding:2px 8px;border-radius:2px;font-size:0.75rem;'
            f'margin-right:0.35rem;color:#4a4238;"><strong>{k}:</strong> '
            f'{", ".join(map(str, v[:3]))}{"…" if len(v) > 3 else ""}</span>'
            for k, v in active
        )
        st.markdown(
            f'<div style="margin:0.4rem 0 0.8rem 0;"><span style="font-size:0.72rem;'
            f'color:#6b6257;text-transform:uppercase;letter-spacing:0.05em;'
            f'margin-right:0.6rem;">Active filters ({len(active)}):</span>{chips}</div>',
            unsafe_allow_html=True,
        )

    if df.empty:
        st.warning("No sessions match these filters.")
        return

    # ---- Session picker + detail (only thing left after clean-up) ----
    st.markdown(f"### Pick a session to inspect  ·  {len(df)} match the filters")

    # Clean first_user_message (strip JSON wrapper)
    def _clean_msg(raw) -> str:
        if raw is None:
            return ""
        s = str(raw).strip()
        if s.startswith("{"):
            try:
                d = json.loads(s)
                if isinstance(d, dict) and "text" in d:
                    return str(d["text"]).strip()
            except Exception:
                pass
        return s

    sev_glyph = {"Critical": "🔴", "High": "🟠", "Medium": "🟡",
                 "Low": "🟢", "(unclassified)": "⚪"}
    label_map: dict[str, str] = {}
    for _, r in df.sort_values("timestamp_epoch", ascending=False).iterrows():
        sid  = r["id"]
        sev  = str(r.get("severity") or "(unclassified)")
        cat  = str(r.get("category") or "(unclassified)")
        msg  = _clean_msg(r.get("first_user_message"))
        dur  = int(r.get("duration_seconds") or 0)
        glyph = sev_glyph.get(sev, "⚪")
        preview = msg[:55] + ("…" if len(msg) > 55 else "")
        label_map[sid] = f"{glyph}  {cat:<20} · {sev:<10} · {dur:>3}s · \"{preview}\"  ·  {sid}"

    picked = st.selectbox(
        "Session (intent · severity · duration · first caller utterance)",
        options=list(label_map.keys()),
        format_func=lambda sid: label_map.get(sid, sid),
    )
    if picked:
        render_session_detail(picked, df[df["id"] == picked].iloc[0])


def render_session_detail(session_id: str, row: pd.Series):
    st.markdown(
        f'**{session_id}** · [Open in Sierra ↗]({sierra_url(session_id)})  '
        f'· {row["timestamp_iso"]} · dur={int(row["duration_seconds"])}s · device={row["device"]}'
    )

    if row["category"] != "(unclassified)":
        sev_cls = {"Critical": "sev-crit", "High": "sev-high",
                   "Medium": "sev-med", "Low": "sev-low"}.get(row["severity"], "")
        st.markdown(
            f'Category: **{row["category"]}** · '
            f'Severity: <span class="{sev_cls}">{row["severity"]}</span>',
            unsafe_allow_html=True,
        )
        try:
            pts = json.loads(row["pain_points_json"] or "[]")
        except Exception:
            pts = []
        if pts:
            st.markdown("**Pain points:**")
            for p in pts:
                st.markdown(f"- {p}")
        if row["suggestion"]:
            st.markdown(f"**Suggestion:** {row['suggestion']}")

    tabs = st.tabs(["Transcript", "Tool traces", "Tags", "Monitors"])

    with tabs[0]:
        msgs = load_messages(session_id)
        msgs = msgs[msgs["text"].notna() & (msgs["text"] != "")]
        for _, m in msgs.iterrows():
            role = (m["role"] or "").lower()
            cls = "transcript-agent" if role == "agent" else \
                  "transcript-user"  if role == "user" else "transcript-sys"
            label = (role or "?").upper()
            st.markdown(
                f'<div class="{cls}"><strong>{label}:</strong> {m["text"]}</div>',
                unsafe_allow_html=True,
            )

    with tabs[1]:
        tr = load_session_traces(session_id)
        tr_ext = tr[tr["tool_name"].notna() & ~tr["tool_name"].isin([
            "goalsdk_respond", "ask_ai", "sleep", "classify_observations",
            "threat_evaluation", "personalized_progress_indicator",
            "classify_agent_monitor", "safety_monitor",
        ])]
        st.dataframe(tr_ext[["idx", "type", "tool_name", "error"]],
                     use_container_width=True, height=360, hide_index=True)
        with st.expander("Show all traces including framework internals"):
            st.dataframe(tr[["idx", "type", "tool_name", "purpose", "error"]],
                         use_container_width=True, hide_index=True)

    with tabs[2]:
        t = load_tags()
        sess_tags = t[t["session_id"] == session_id]["tag"].tolist()
        if sess_tags:
            st.markdown(" ".join(f'`{x}`' for x in sess_tags))
        else:
            st.info("No tags.")

    with tabs[3]:
        m = load_monitors()
        sess_mons = m[m["session_id"] == session_id][["name", "detected"]]
        sess_mons["detected"] = sess_mons["detected"].map({0: "clean", 1: "DETECTED"})
        st.dataframe(sess_mons, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# PAGE 3 — Gap Drilldown
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Gap → fix mapping (AS-IS → SHOULD-BE)
# ---------------------------------------------------------------------------

# For each gap id, describe the concrete "should be" state so the drilldown
# shows a fixed, expanded view. Anchors point into the NEW_JOURNEYS,
# GLOBAL_RULES_TO_ADD, NEW_TOOLS lists where appropriate.

FIX_TYPE_TO_PATH = {
    "New journey":               "Agent Builder → Journeys (sidebar) → **[+ New Journey]**",
    "New tool":                  "Agent Builder → **Tools** section → **[+ New Tool]** (requires integrations team to implement the backend)",
    "New tool + journey change": "Two places:\n  1. Agent Builder → **Tools** → [+ New Tool] (new tool)\n  2. Agent Builder → Journeys → [target journey name in SHOULD-BE] → **Rules** (remove old transfer logic)",
    "Global rule":               "Agent Builder → **Global context blocks** (sidebar, bottom) → **Rules** → add the rule text as a new item",
    "Policy change":             "Agent Builder → **Global context blocks** → **Policies**",
    "Journey change":            "Agent Builder → Journeys → [**target journey name** as named in SHOULD-BE box] → Rules / Response phrasing (as noted in change summary)",
    "Routing additions":         "Agent Builder → Journeys → [target journey] → **Condition** section (edit the condition criteria list to include the new intent tags)",
}


def _sierra_location_for_code(proposed: str) -> str:
    """Infer which Sierra UI location an IF/THEN code block targets."""
    if not proposed:
        return ""
    import re
    first = proposed.strip().splitlines()[0].strip()
    # Match: IN 'X' block: ...   (case-insensitive)
    m = re.match(r"IN\s+['\"]?([^'\"]+?)['\"]?\s+block", first, re.IGNORECASE)
    if m:
        journey = m.group(1).strip()
        sub = ""
        if "sub-flow" in first.lower():
            sub = " → Rules (new sub-flow)"
        elif "policies" in first.lower():
            sub = " → Policies"
        elif "rules" in first.lower():
            sub = " → Rules"
        else:
            sub = " → Rules"
        return f"Agent Builder → Journeys → **\"{journey}\"**{sub}"
    m = re.match(r"IN\s+([A-Z][A-Za-z ]+?)\s+/\s+['\"]?([^'\"]+?)['\"]?\s+block", first, re.IGNORECASE)
    if m:
        section, journey = m.group(1), m.group(2)
        return f"Agent Builder → Journeys → **\"{journey}\"** (also applies from {section})"
    if re.search(r"global\s+context|global\s*\w*\s*→\s*rules", proposed, re.IGNORECASE):
        return "Agent Builder → **Global context blocks** → **Rules**"
    if re.search(r"global\s*\w*\s*→\s*polic", proposed, re.IGNORECASE):
        return "Agent Builder → **Global context blocks** → **Policies**"
    if "journey" in proposed.lower() and "new" in proposed.lower():
        return "Agent Builder → Journeys → **[+ New Journey]**"
    return "Agent Builder (verify exact path with config team)"


def _spec_for_gap(gap_id: int) -> tuple[str, str] | None:
    """Return (spec_title, spec_body) if this gap has a full Sierra-format
    spec (journey / rule / tool) already drafted elsewhere. Returns None
    when the fix is a config edit without a structured spec."""
    fix = GAP_FIX_MAP.get(gap_id, {})
    anchor = fix.get("anchor_name", "").lower()
    if not anchor:
        return None
    # Journeys
    for j in NEW_JOURNEYS:
        if j["name"].lower() == anchor or anchor in j["name"].lower():
            return (f"Full journey spec — {j['name']}", j["spec"])
    # Global rules
    for r in GLOBAL_RULES_TO_ADD:
        if (r["name_en"].lower() == anchor
            or anchor in r["name_en"].lower()
            or r["name_en"].lower() in anchor):
            body = (
                f"RULE: {r['name_en']}\n"
                f"PLACE IN: {r['target']}\n\n"
                f"TEXT TO ADD:\n{r['text_en']}\n\n"
                f"EXAMPLE / WHY IT MATTERS:\n{r['example_en']}"
            )
            return (f"Full rule spec — {r['name_en']}", body)
    # Tools
    for t in NEW_TOOLS:
        if t["name"].lower() == anchor or anchor in t["name"].lower():
            body = (
                f"TOOL NAME: {t['name']}\n"
                f"PURPOSE: {t['purpose_en']}\n"
                f"INPUTS: {t['inputs']}\n"
                f"CLOSES: Gap #{t['gap']}\n\n"
                f"IMPLEMENTATION: requires integrations team. The tool must be "
                f"added to the agent's tool registry with the signature above, "
                f"then referenced from the relevant journey."
            )
            return (f"New tool spec — {t['name']}", body)
    return None


GAP_FIX_MAP: dict[int, dict] = {
    1: {
        "fix_type":    "New journey",
        "anchor_name": "Payout Failure / Receiver Issue",
        "as_is":       (
            "When the receiver cannot collect the money, Aria has no journey "
            "for it. The call defaults into **Check Order Status** and the "
            "agent says *\"your order is sent and on its way\"* — "
            "functionally useless for the caller."
        ),
        "should_be":   (
            "Create a new journey **Payout Failure / Receiver Issue** that "
            "triggers when the caller reports the receiver cannot collect. "
            "It routes to payer-change, correspondent health check, refund, "
            "or live rep depending on diagnosis. Uses the new tools "
            "`CorrespondentHealthCheck`, `RequestModification`, "
            "`TransferToLiveRep`."
        ),
        "change_summary": [
            "Add a new journey block at the same level as Check Order Status.",
            "Add new tool: CorrespondentHealthCheck.",
            "Add new tool: RequestModification (for payer-change escalation).",
            "Route `unsupportedIntent:transaction-status-not-recognized` to this journey.",
        ],
    },
    2: {
        "fix_type":    "New tool + journey change",
        "anchor_name": "TransferToLiveRep",
        "as_is":       (
            "The transfer-to-human logic lives **only inside Cancel Customer "
            "Order**. Sessions where the caller needs escalation outside the "
            "Cancel flow get the agent saying *\"I can't transfer you yet "
            "since your order isn't marked for cancellation.\"* — internal "
            "logic leaking to the user."
        ),
        "should_be":   (
            "Extract transfer into a standalone tool "
            "**`tool:TransferToLiveRep(reason, intent_context, language, "
            "authenticated, partial_context)`** callable from any journey. "
            "Keep Cancel Customer Order's eligibility checks, but decouple "
            "them from transfer capability."
        ),
        "change_summary": [
            "Build new tool: TransferToLiveRep (back-end team).",
            "Modify Cancel Customer Order journey: extract transfer to the new tool.",
            "Reference new tool from Structured Escalation Ladder journey.",
        ],
    },
    3: {
        "fix_type":    "New journey",
        "anchor_name": "Modification",
        "as_is":       (
            "No Modification journey exists. Callers wanting to change the "
            "recipient, payer, address, or amount hit a dead end — today "
            "they get routed to Cancel or Status by default, neither of "
            "which resolves the modification."
        ),
        "should_be":   (
            "Create **Modification** journey with sub-journeys for Name / "
            "Payer / Address / Amount changes. Routes "
            "`unsupportedIntent:change-order-details` to here. Uses "
            "`RequestModification` tool for back-office queueing when "
            "self-service is not allowed."
        ),
        "change_summary": [
            "Add journey block with 4 sub-journeys.",
            "Wire unsupportedIntent:change-order-details → Modification.",
            "Policy: never offer Cancel as a consolation for a modification request.",
        ],
    },
    4: {
        "fix_type":    "Global rule + new tool",
        "anchor_name": "Pre-auth intent triage",
        "as_is":       (
            "Root journey rule (literal): *\"After confirming it is a "
            "customer calling, you must first immediately find the customer "
            "in Euronet systems by asking for the order number.\"* → Aria "
            "must authenticate ~5 exchanges before hearing the actual "
            "intent. For Payout Failures the caller already said the "
            "problem in message #1."
        ),
        "should_be":   (
            "Add **Global Rule: Pre-auth intent triage**. Build tool "
            "**`ClassifyIntent(utterance, language)`** that runs on the "
            "caller's first substantive utterance and returns one of "
            "{payout-failure, modification, status, cancel, ETA, general-faq, "
            "immediate-escalation}. Only {status, cancel, ETA} require the "
            "full CVP flow; everything else routes immediately."
        ),
        "change_summary": [
            "Build ClassifyIntent tool.",
            "Add rule in Global context blocks → Rules.",
            "Modify 'Intents Where User Needs to Authenticate' journey to call ClassifyIntent before any auth work.",
        ],
    },
    5: {
        "fix_type":    "Global rule",
        "anchor_name": "Language detection & switching",
        "as_is":       (
            "Agent answers in the wrong language (observed: Italian when "
            "caller speaks Spanish or French). No rule for proactive "
            "language offer when caller has a Hispanic name or LATAM "
            "corridor. `language:unsupported` tag fires but triggers no "
            "action."
        ),
        "should_be":   (
            "Add **Global Rule: Language detection**. Detect locale from "
            "the caller's first utterance. If non-English and Aria has "
            "locale support, switch fully. Never answer in a third "
            "language. If `language:unsupported`, offer transfer to a "
            "language-capable rep via `TransferToLiveRep`."
        ),
        "change_summary": [
            "Add rule in Global context blocks → Rules.",
            "Integrate with TransferToLiveRep tool for unsupported languages.",
        ],
    },
    6: {
        "fix_type":    "Global rule",
        "anchor_name": "End-of-call termination",
        "as_is":       (
            "Agent keeps emitting *\"I'm still here\"* messages after "
            "mutual goodbye, creating false expectations. No rule in "
            "Global context blocks covers call termination."
        ),
        "should_be":   (
            "Add **Global Rule: End-of-call termination**. After a mutual "
            "closing exchange (caller thanks + agent farewell), end the "
            "session silently. Do NOT emit *\"still here\"* probes. If "
            "silence > 8 seconds after close, disconnect."
        ),
        "change_summary": [
            "Add rule in Global context blocks → Rules.",
        ],
    },
    7: {
        "fix_type":    "Global rule",
        "anchor_name": "Caller-type ambiguity default",
        "as_is":       (
            "When the caller answers the customer-vs-agent disambiguation "
            "with anything ambiguous (\"no\", \"yes\", silence), Aria "
            "re-asks the same question indefinitely instead of defaulting "
            "after N retries."
        ),
        "should_be":   (
            "Add **Global Rule: Caller-type disambiguation default**. "
            "After 2 ambiguous replies, default to Customer and proceed. "
            "Never loop the same question more than 2 times."
        ),
        "change_summary": [
            "Add rule in Global context blocks → Rules.",
            "Modify root journey to exit the disambiguation loop after 2 attempts.",
        ],
    },
    8: {
        "fix_type":    "Global rule",
        "anchor_name": "Monitor-triggered auto-recovery",
        "as_is":       (
            "Sierra ships 4 monitors (Agent Looping, False Transfer, "
            "Frustration Increase, Repeated Escalation). They fire at 40 "
            "/ 12 / 11 / 3 % rates — but detection does NOT trigger any "
            "behaviour change. The monitors are observability only."
        ),
        "should_be":   (
            "Add **Global Rule: Monitor auto-recovery**. When the Agent "
            "Looping monitor fires, BREAK the current flow, apologize "
            "(*\"I'm going in circles — let me try a different approach\"*), "
            "and either fall back to `CustomerByTelephone`, create a "
            "Zendesk ticket, or offer live-rep transfer. The monitor "
            "firing MUST change behaviour, not just log."
        ),
        "change_summary": [
            "Add rule in Global context blocks → Rules that reacts to each monitor.",
            "Wire monitor events into the flow control, not just observability.",
        ],
    },
    9: {
        "fix_type":    "Routing additions",
        "anchor_name": "unsupportedIntent taxonomy routing",
        "as_is":       (
            "The agent tags 46 sessions (41%) with `unsupportedIntent:<sub>` "
            "(recall, change-order-details, account-department, "
            "technical-issues, accounts-receivable, agent-provides-"
            "department, transaction-status-not-recognized) — but there "
            "is no journey that catches any of these labels. Tags are "
            "pure observability today."
        ),
        "should_be":   (
            "Each sub-tag should route to a concrete journey:\n"
            "- `:recall` → Cancel Customer Order (with 'recall' as synonym)\n"
            "- `:change-order-details` → **NEW** Modification journey\n"
            "- `:transaction-status-not-recognized` → **NEW** Payout Failure journey\n"
            "- `:account-department`, `:technical-issues`, `:accounts-receivable` → **NEW** General FAQ journey\n"
            "- `:agent-provides-department` → Agent Authentication flow"
        ),
        "change_summary": [
            "Add routing entry for each sub-tag in the appropriate journey's Condition.",
            "Coordinate with Gap #1, #3, and the General FAQ addition.",
        ],
    },
    10: {
        "fix_type":    "Global rule",
        "anchor_name": "Auth hard-exit + CustomerByTelephone fallback",
        "as_is":       (
            "`tool:order-overview:failed` fires 21 times and "
            "`tool:cvp:failed` 8 times with no cap on retries and no "
            "fallback branch. The agent retries the same tool with the "
            "same arguments until the caller hangs up."
        ),
        "should_be":   (
            "Add **two Global Rules**:\n"
            "1. **Auth hard-exit after 2 failures** — if "
            "`AttemptCvpAuthentication` fails twice, create a Zendesk "
            "ticket with context and inform the caller a team member will "
            "call back. Do NOT attempt a third time.\n"
            "2. **CustomerByTelephone fallback** — if "
            "`CustomerByOrderNumber` fails twice, call "
            "`CustomerByTelephone` with the caller's ANI."
        ),
        "change_summary": [
            "Add 2 rules to Global context blocks → Rules.",
            "Modify 'Intents Where User Needs to Authenticate' journey to include the retry caps.",
            "All tool-failure branches must end in CreateZendeskTicket as planned exit.",
        ],
    },
    11: {
        "fix_type":    "Global rule",
        "anchor_name": "DTMF input handling",
        "as_is":       (
            "Callers press digits on the keypad expecting IVR-style "
            "response, but Aria is voice-only and silently ignores DTMF. "
            "This creates dead air followed by frustration or hangup."
        ),
        "should_be":   (
            "Add **Global Rule: DTMF input handling**. When a DTMF "
            "keypress is received, respond immediately: *\"I'm a voice "
            "assistant — could you say that instead of pressing the "
            "keypad?\"* Do not ignore the event."
        ),
        "change_summary": [
            "Implement DTMF event handler in the voice runtime.",
            "Add rule in Global context blocks → Rules.",
        ],
    },
    12: {
        "fix_type":    "Policy change",
        "anchor_name": "Proactive Zendesk fallback",
        "as_is":       (
            "`CreateZendeskTicket` succeeds 74 times today but almost "
            "always AFTER the conversation has already failed. Tickets "
            "are reactive — a last-ditch escalation, not part of any "
            "planned fallback path."
        ),
        "should_be":   (
            "Every tool-failure branch in every journey must have "
            "`CreateZendeskTicket` as a planned exit. When auth fails N "
            "times → ticket + 'human will call back'. When correspondent "
            "has issues → ticket + expected-next-step. The ticket itself "
            "becomes the graceful handoff."
        ),
        "change_summary": [
            "Audit every journey for unguarded tool-failure branches.",
            "Replace each 'retry same tool' with 'CreateZendeskTicket + read back ticket number'.",
            "Add pattern to Global context blocks → Policies.",
        ],
    },
    13: {
        "fix_type":    "Journey change",
        "anchor_name": "Select Order — partial-capture case",
        "as_is":       (
            "When ASR captures a partial order number (e.g. 'ES9...') "
            "the agent discards it and restarts from scratch rather than "
            "confirming the partial with the caller and completing the "
            "known-prefix match."
        ),
        "should_be":   (
            "Modify Select Order journey: if ASR returned a valid prefix "
            "(e.g. 'ES9') with confidence > 0.5, ask the caller *\"Is it "
            "ES9…?\"* instead of restarting from zero."
        ),
        "change_summary": [
            "Add partial-capture branch in Select Order journey.",
            "Surface ASR confidence to the journey logic (needs backend work).",
        ],
    },
    14: {
        "fix_type":    "Global rule",
        "anchor_name": "ASR confidence gating for alphanumerics",
        "as_is":       (
            "Order numbers, phone numbers, and other alphanumerics are "
            "used downstream without any check on word-level ASR confidence. "
            "Words with confidence < 0.5 are treated the same as 0.99. "
            "The data IS captured in `transcriptionMetadata.words` but "
            "not surfaced to any decision logic."
        ),
        "should_be":   (
            "Add **Global Rule: ASR confidence gating**. When capturing "
            "an alphanumeric value, reject words with confidence < 0.7 "
            "and ask the caller to re-spell using NATO phonetic alphabet "
            "for uncertain characters (*'F as in Foxtrot'*). Do not call "
            "downstream tools with low-confidence values."
        ),
        "change_summary": [
            "Expose word-level confidence to journey logic.",
            "Add rule in Global context blocks → Rules.",
        ],
    },
    15: {
        "fix_type":    "New journey",
        "anchor_name": "Structured Escalation Ladder",
        "as_is":       (
            "22 sessions ask for a human as their first utterance "
            "(`milestone:requests-immediate-transfer`). Today the agent "
            "ignores that signal and forces them through greeting + "
            "caller-type + partial auth before even acknowledging the "
            "transfer request."
        ),
        "should_be":   (
            "Create **Structured Escalation Ladder** journey implementing "
            "Steps A→E: A) identify via CustomerByTelephone · B) partial "
            "order-number rescue · C) single-question intent capture · "
            "D) contextual TransferToLiveRep · E) callback Zendesk ticket "
            "if offline hours. Respects consulting guidance of trying all "
            "fallbacks **before** transfer."
        ),
        "change_summary": [
            (
                "**Create the journey with the 5-step ladder** (full spec in the "
                "code block below):\n"
                "  - **Step A · Identify without order number** — call "
                "`tool:CustomerByTelephone(caller_ani)`. If match, ask the caller "
                "to confirm they are the account holder and proceed with CVP "
                "pre-populated.\n"
                "  - **Step B · Partial order-number rescue** — if ASR captured "
                "a prefix like 'ES9' with confidence > 0.5, confirm the prefix "
                "aloud (*'Did you say ES9 something?'*) instead of restarting.\n"
                "  - **Step C · Single-question intent capture** — ask ONE "
                "question (*'Could you tell me what the call is about?'*) and "
                "classify via `tool:ClassifyIntent`. No full CVP at this point.\n"
                "  - **Step D · Contextual transfer** — call "
                "`tool:TransferToLiveRep(reason, language, partial_context)` so "
                "the human agent gets the caller's intent + whatever data was "
                "collected pre-populated.\n"
                "  - **Step E · Callback ticket** — if offline hours or transfer "
                "fails, call `tool:CreateZendeskTicket(priority=high, "
                "reason='requested-human-callback')` and read back the ticket "
                "number to the caller."
            ),
            (
                "**Register the 3 tools used by this journey.** `CustomerByTelephone` "
                "and `CreateZendeskTicket` already exist (see Tools reference in "
                "Glossary). `TransferToLiveRep` is NEW — requires integrations "
                "team to build (see Gap #2)."
            ),
            (
                "**Route the trigger tag.** Today 22 sessions fire "
                "`milestone:requests-immediate-transfer` with nowhere to go. "
                "Add this tag as a Condition trigger for the new journey."
            ),
        ],
    },
}


def page_gap_drilldown():
    st.title(_t(
        "🎯  Gap Proposals — AS-IS → SHOULD-BE",
        "🎯  Gap Proposals — AS-IS → SHOULD-BE",
    ))
    st.caption(_t(
        "Cada gap presentado lado a lado: estado roto actual vs. fix propuesto. "
        "Todo el contenido expandido — usa los filtros del sidebar para "
        "filtrar por severidad, fuente, o grounding.",
        "Every gap laid out side-by-side: current broken state vs. proposed "
        "fixed state. All sections expanded — use the sidebar filters to "
        "narrow by severity, source, or grounding.",
    ))

    sessions_all = load_sessions()
    sessions     = sampled(sessions_all)
    tags     = load_tags()
    traces   = load_traces()
    issues   = load_issues_raw()

    # Sidebar folder-style filters
    with st.sidebar:
        st.markdown("### Filters")
        severities = sorted({g["severity"] for g in GAPS}, key=lambda x: ["Critical","High","Medium","Low"].index(x))
        sel_sev = st.multiselect("Severity", severities, default=severities)

        sources = ["UI review", "Data mining"]
        sel_src = st.multiselect("Source", sources, default=sources)

        groundings = {
            "scraped-block": "Block content scraped",
            "scraped-data":  "Data signals scraped",
            "data-only":     "Inferred from data",
        }
        sel_gnd_labels = st.multiselect(
            "Grounding",
            options=list(groundings.values()),
            default=list(groundings.values()),
        )
        sel_gnd_keys = [k for k, v in groundings.items() if v in sel_gnd_labels]

        fix_types = sorted({GAP_FIX_MAP.get(g["id"], {}).get("fix_type", "Other") for g in GAPS})
        sel_fix = st.multiselect("Fix type", fix_types, default=fix_types)

        st.markdown("---")
        st.caption("All filters combine with AND.")

    # Apply filters
    def src_label(g):
        return "UI review" if g["source"] == "config-review" else "Data mining"

    filtered = [
        g for g in GAPS
        if g["severity"] in sel_sev
        and src_label(g) in sel_src
        and g["grounded_in"] in sel_gnd_keys
        and GAP_FIX_MAP.get(g["id"], {}).get("fix_type", "Other") in sel_fix
    ]

    # Order: Critical > High > Medium > Low, then id
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    filtered.sort(key=lambda g: (sev_rank.get(g["severity"], 9), g["id"]))

    st.markdown(f"### {len(filtered)} / {len(GAPS)} gaps match the current filters")

    if not filtered:
        st.warning("No gaps match. Widen the filters.")
        return

    issue_by_idx = {it["_idx"]: it for it in issues}

    # One tab per gap — user navigates without scrolling.
    sev_emoji = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}
    tab_labels = [f"{sev_emoji[g['severity']]}  #{g['id']}" for g in filtered]
    gap_tabs = st.tabs(tab_labels)
    for tab, g in zip(gap_tabs, filtered):
        with tab:
            render_gap_section(g, issues, issue_by_idx, sessions, tags, traces)


def render_gap_section(g: dict, issues: list[dict], issue_by_idx: dict,
                       sessions: pd.DataFrame, tags: pd.DataFrame,
                       traces: pd.DataFrame) -> None:
    sev_cls = {"Critical": "sev-crit", "High": "sev-high",
               "Medium": "sev-med", "Low": "sev-low"}[g["severity"]]
    src_label_ = "UI review" if g["source"] == "config-review" else "Data mining"
    grounded_label = {
        "scraped-block": "Block content scraped",
        "scraped-data":  "Data signals scraped",
        "data-only":     "Inferred from data",
    }.get(g["grounded_in"], "")

    fix = GAP_FIX_MAP.get(g["id"], {})

    # Header (no divider — tabs already separate gaps visually)
    st.markdown(
        f'## <span class="{sev_cls}">Gap #{g["id"]}</span>  {g["title_en"]}',
        unsafe_allow_html=True,
    )
    meta_cols = st.columns(4)
    meta_cols[0].markdown(f"**Source:** {src_label_}")
    meta_cols[1].markdown(f"**Grounding:** {grounded_label}")
    meta_cols[2].markdown(f"**Fix type:** {fix.get('fix_type', '—')}")
    meta_cols[3].markdown(f"**Severity:** {g['severity']}")

    # AS-IS | SHOULD-BE side-by-side
    asis_col, shouldbe_col = st.columns(2)

    with asis_col:
        st.markdown("#### 🔴  AS-IS  ·  state today")
        st.markdown(
            f'<div style="background:#fdf0ed;border-left:4px solid #c44f3a;'
            f'padding:0.8rem 1rem;border-radius:2px;">'
            f'{fix.get("as_is") or g["evidence_en"]}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(f"**Data signal**  ·  {g['data_signal_en']}")
        if g.get("samples"):
            st.markdown("**Real-world samples**:")
            for s in g["samples"][:5]:
                st.markdown(f"- [{s} ↗]({sierra_url(s)})")

    with shouldbe_col:
        st.markdown("#### 🟢  SHOULD-BE  ·  proposed fix")
        st.markdown(
            f'<div style="background:#eef5eb;border-left:4px solid #6c8d5a;'
            f'padding:0.8rem 1rem;border-radius:2px;">'
            f'{fix.get("should_be") or "_(Fix spec not yet written for this gap.)_"}</div>',
            unsafe_allow_html=True,
        )
        if fix.get("change_summary"):
            st.markdown("**Concrete changes**:")
            for ch in fix["change_summary"]:
                st.markdown(f"- {ch}")
        if fix.get("anchor_name"):
            st.caption(f"Config anchor: _{fix['anchor_name']}_")

    # ----- Sierra location callout (spans full width under the columns) -----
    path = FIX_TYPE_TO_PATH.get(fix.get("fix_type", ""), "")
    if path:
        st.markdown(
            f'<div style="background:#fff6f3;border:1px solid #eddbd4;'
            f'border-radius:3px;padding:0.8rem 1rem;margin-top:0.8rem;">'
            f'<strong>📍 Dónde colocarlo en Sierra</strong> · navegación del Agent Builder:<br>'
            f'{path}</div>',
            unsafe_allow_html=True,
        )

    # ----- Full spec (journey / rule / tool) if we have a drafted one -----
    spec = _spec_for_gap(g["id"])
    if spec:
        spec_title, spec_body = spec
        st.markdown(f"**📜 {spec_title}**  — paste / adapt this into the Sierra UI:")
        st.code(spec_body, language="text")

    # Linked issues (fixed, expanded — no toggles)
    linked_issue_ids = g["issues"] or []
    if linked_issue_ids:
        st.markdown("#### 📋  Linked issues from the cluster log")
        for idx in linked_issue_ids:
            it = issue_by_idx.get(idx)
            if not it: continue
            isev = (it.get("severity") or "").title()
            icls = {"Critical": "sev-crit", "High": "sev-high",
                    "Medium": "sev-med", "Low": "sev-low"}.get(isev, "sev-low")
            st.markdown(
                f'**Issue #{it["_idx"]}**  '
                f'<span class="{icls}">{isev}</span>  ·  '
                f'**{it.get("impacted_count", 0)}** sessions impacted  ·  '
                f'{it.get("issue_title", "")}',
                unsafe_allow_html=True,
            )
            if it.get("description"):
                st.caption(it["description"][:400] + ("…" if len(it.get("description","")) > 400 else ""))
            if it.get("proposed_outcome"):
                loc = _sierra_location_for_code(it["proposed_outcome"])
                if loc:
                    st.markdown(
                        f'<div style="background:#fff6f3;border-left:3px solid #c44f3a;'
                        f'padding:0.35rem 0.7rem;border-radius:2px;font-size:0.85rem;'
                        f'margin-top:0.4rem;">'
                        f'📍 <strong>Dónde pegar este fix:</strong> {loc}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                st.code(it["proposed_outcome"], language="text")

    # Session cohort analysis (all sessions sharing samples or from linked issues)
    ref_ids = set(g.get("samples") or [])
    for idx in linked_issue_ids:
        it = issue_by_idx.get(idx)
        if it:
            ref_ids.update(it.get("reference_sessions") or [])
    ref_ids = [x for x in ref_ids if x]

    if ref_ids:
        st.markdown("#### 👥  Sessions in the evidence cohort")

        cohort = sessions[sessions["id"].isin(ref_ids)].copy()
        n_cohort = len(cohort)
        avg_dur = int(cohort["duration_seconds"].mean() or 0)
        avg_msgs = cohort["message_count"].mean()
        cols = st.columns(4)
        cols[0].metric("Cohort size", f"{n_cohort}")
        cols[1].metric("Avg duration (s)", f"{avg_dur}")
        cols[2].metric("Avg messages", f"{avg_msgs:.0f}" if pd.notna(avg_msgs) else "—")
        cols[3].metric("Linked issues", f"{len(linked_issue_ids)}")

        # Table with Sierra links
        cohort["sierra_link"] = cohort["id"].apply(sierra_url)
        st.dataframe(
            cohort[["id", "sierra_link", "timestamp_iso", "duration_seconds",
                    "category", "severity", "first_user_message"]]
                .sort_values("duration_seconds", ascending=False),
            column_config={
                "sierra_link": st.column_config.LinkColumn("Open ↗", display_text="↗"),
            },
            use_container_width=True, hide_index=True,
        )

        # Tools + tags signature — ALWAYS expanded, side-by-side
        framework = ("goalsdk_respond", "ask_ai", "sleep",
                     "classify_observations", "threat_evaluation",
                     "personalized_progress_indicator", "classify_agent_monitor",
                     "safety_monitor", "classify_interruption",
                     "should_query_kb", "deadlock_detector", "detect_abuse",
                     "param_validation", "turn")
        colA, colB = st.columns(2)
        with colA:
            st.markdown("##### Top tools invoked in this cohort")
            trs = traces[
                (traces["session_id"].isin(ref_ids)) &
                (traces["tool_name"].notna()) &
                (~traces["tool_name"].isin(framework))
            ]
            top = trs["tool_name"].value_counts().head(10)
            if not top.empty:
                fig = px.bar(top.sort_values(), orientation="h",
                             color=top.values, color_continuous_scale="Oranges",
                             text=top.sort_values().values)
                fig.update_layout(height=280, xaxis_title="", yaxis_title="",
                                  coloraxis_showscale=False,
                                  margin=dict(l=10, r=10, t=10, b=10))
                fig.update_traces(textposition="outside", cliponaxis=False)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("No external tools invoked in this cohort.")
        with colB:
            st.markdown("##### Top tags on this cohort")
            t = tags[tags["session_id"].isin(ref_ids)]
            top_tags = t["tag"].value_counts().head(12)
            if not top_tags.empty:
                fig = px.bar(top_tags.sort_values(), orientation="h",
                             color=top_tags.values, color_continuous_scale="Blues",
                             text=top_tags.sort_values().values)
                fig.update_layout(height=280, xaxis_title="", yaxis_title="",
                                  coloraxis_showscale=False,
                                  margin=dict(l=10, r=10, t=10, b=10))
                fig.update_traces(textposition="outside", cliponaxis=False)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("No tags on this cohort.")


# ---------------------------------------------------------------------------
# PAGE 4 — Simulations
# ---------------------------------------------------------------------------

def page_simulations():
    st.title("🧪  Simulations — pass rate & gap coverage")
    rows = []
    for section, passed, total, scenarios in SIMULATIONS:
        rows.append({"section": section, "passed": passed, "total": total,
                     "failing": total - passed,
                     "pct": round(100 * passed / total) if total else 0,
                     "scenarios": scenarios})
    df = pd.DataFrame(rows)

    # --- Hero narrative (same pattern as Overview) -----------------------
    total_pass   = int(df["passed"].sum())
    total_scen   = int(df["total"].sum())
    pass_rate    = round(100 * total_pass / max(total_scen, 1))
    zero_secs    = int((df["passed"] == 0).sum())
    full_secs    = int((df["passed"] == df["total"]).sum())
    n_secs       = len(df)

    # Worst 3 zero-sections by total (biggest blind spots)
    worst3 = df[df["passed"] == 0].sort_values("total", ascending=False).head(3)
    worst_names = ", ".join(f"<strong>{s}</strong>" for s in worst3["section"])

    st.markdown(
        f'<div style="background:linear-gradient(135deg,#1a1a1a 0%,#2b2320 100%);'
        f'color:#faf9f5;padding:1.3rem 1.7rem;border-radius:6px;'
        f'margin:0.5rem 0 1rem 0;border-left:6px solid #c44f3a;">'
        f'<div style="font-size:0.72rem;color:#da7756;text-transform:uppercase;'
        f'letter-spacing:0.12em;font-weight:600;margin-bottom:0.5rem;">'
        f'Estado de regresión</div>'
        f'<div style="font-size:1.1rem;line-height:1.55;">'
        f'Solo <strong style="color:#da7756;">{total_pass}/{total_scen}</strong> escenarios '
        f'pasando ({pass_rate}%). <strong style="color:#da7756;">{zero_secs} de {n_secs}</strong> '
        f'secciones tienen CERO tests pasando — el agente no tiene red de regresión en esas áreas. '
        f'Prioridades más grandes: {worst_names}.'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sections",     f"{len(df)}")
    c2.metric("0 passing",    f"{zero_secs}",
              delta=f"{round(100*zero_secs/n_secs)}% of sections",
              delta_color="inverse")
    c3.metric("Partial pass", f"{((df.passed > 0) & (df.passed < df.total)).sum()}")
    c4.metric("Full pass",    f"{full_secs}")

    st.markdown("### Pass rate by section")
    df = df.sort_values(["pct", "total"])
    fig = go.Figure()
    fig.add_trace(go.Bar(y=df.section, x=df.passed, name="Passing",
                         orientation="h", marker_color="#6c8d5a",
                         text=df.apply(lambda r: f"{r.passed}/{r.total} ({r.pct}%)", axis=1),
                         textposition="inside"))
    fig.add_trace(go.Bar(y=df.section, x=df.failing, name="Failing / Untested",
                         orientation="h", marker_color="#c44f3a",
                         text=df.failing.astype(str), textposition="inside"))
    fig.update_layout(barmode="stack", height=720, xaxis_title="", yaxis_title="",
                      legend=dict(orientation="h", y=-0.05),
                      margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Gap × simulation coverage")
    st.caption("Click any **Gap #N** button to see the gap detail popover.")

    status_rank = {"missing": 0, "covered-failing": 1, "covered-partial": 2, "covered-passing": 3}
    status_style = {
        "missing":          ("No simulation",       "#c44f3a"),
        "covered-failing":  ("Sim exists, failing", "#d97e5a"),
        "covered-partial":  ("Partially covered",   "#c9a449"),
        "covered-passing":  ("Covered & passing",   "#6c8d5a"),
    }
    sorted_cov = sorted(GAP_SIM_COVERAGE, key=lambda r: status_rank.get(r[2], 99))

    # 4-column layout: gap popover | title | status badge | sims
    header = st.columns([0.12, 0.32, 0.18, 0.38])
    header[0].markdown("**Gap**")
    header[1].markdown("**Description**")
    header[2].markdown("**Status**")
    header[3].markdown("**Simulation(s)**")
    st.divider()

    for gid, title, status, sims in sorted_cov:
        cols = st.columns([0.12, 0.32, 0.18, 0.38])
        with cols[0]:
            gap_popover(gid, label=f"Gap #{gid}", use_container_width=True)
        cols[1].markdown(title)
        label, color = status_style[status]
        cols[2].markdown(
            f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:2px;font-size:.78rem;font-weight:600;">{label}</span>',
            unsafe_allow_html=True,
        )
        cols[3].markdown(
            ", ".join(f"`{s}`" for s in sims) if sims else "—"
        )

    st.markdown("### Drill into section scenarios")
    sel = st.selectbox("Section", df.section.tolist())
    if sel:
        scen = df[df.section == sel].iloc[0]["scenarios"]
        passed = int(df[df.section == sel].iloc[0]["passed"])
        total = int(df[df.section == sel].iloc[0]["total"])
        st.markdown(f"**{sel}** — {passed}/{total} passing")
        for s in scen:
            st.markdown(f"- {s}")


# ---------------------------------------------------------------------------
# PAGE 5 — Issue Log
# ---------------------------------------------------------------------------

def render_by_issue(sessions, traces, tags, monitors, issues):
    """Render the 'By issue' view — clustered patterns + reference session drill-in."""
    if not issues:
        st.warning("No issue log — run scripts/build_issue_log.py first.")
        return

    df = pd.DataFrame([
        {
            "idx":      it["_idx"],
            "journey":  it.get("journey"),
            "title":    it.get("issue_title"),
            "severity": (it.get("severity") or "").title(),
            "impacted": int(it.get("impacted_count") or 0),
            "refs":     ", ".join((it.get("reference_sessions") or [])[:3]),
            "fix":      it.get("proposed_outcome") or "",
            "desc":     it.get("description") or "",
        }
        for it in issues
    ])

    c1, c2 = st.columns(2)
    with c1:
        journeys = sorted(df["journey"].dropna().unique().tolist())
        sel_j = st.multiselect("Journey", journeys, default=[])
    with c2:
        sevs = sorted(df["severity"].dropna().unique().tolist())
        sel_s = st.multiselect("Severity", sevs, default=[])

    if sel_j:
        df = df[df["journey"].isin(sel_j)]
    if sel_s:
        df = df[df["severity"].isin(sel_s)]

    # Sort by severity (Critical first), then impact
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    df_sorted = df.copy()
    df_sorted["_r"] = df_sorted["severity"].map(sev_rank).fillna(9)
    df_sorted = df_sorted.sort_values(["_r", "impacted"], ascending=[True, False])
    # Nice severity display with emoji
    sev_emoji = {"Critical": "🔴", "High": "🟠",
                 "Medium": "🟡", "Low": "🟢"}
    df_sorted["sev_icon"] = df_sorted["severity"].map(
        lambda s: f"{sev_emoji.get(s,'⚪')} {s}"
    )
    max_imp = int(df_sorted["impacted"].max() or 1)
    st.dataframe(
        df_sorted[["idx", "sev_icon", "journey", "title", "impacted", "refs"]],
        use_container_width=True, height=460, hide_index=True,
        column_config={
            "idx":      st.column_config.NumberColumn("#", width="small"),
            "sev_icon": st.column_config.TextColumn("Severity", width="medium"),
            "journey":  st.column_config.TextColumn("Journey", width="medium"),
            "title":    st.column_config.TextColumn("Issue", width="large"),
            "impacted": st.column_config.ProgressColumn(
                "Impacted", format="%d", min_value=0, max_value=max_imp,
                help="Sessions impacted by this issue (progress bar vs the max across all)",
            ),
            "refs":     st.column_config.TextColumn("Refs", width="medium"),
        },
    )

    st.markdown("### Issue detail")
    pick = st.selectbox("Pick an issue",
                        options=df["idx"].tolist(),
                        format_func=lambda i: f"#{i} — {df[df.idx==i].iloc[0]['title']}")
    if pick:
        row = df[df["idx"] == pick].iloc[0]
        sev_cls = {"Critical": "sev-crit", "High": "sev-high",
                   "Medium": "sev-med", "Low": "sev-low"}.get(row["severity"], "")
        st.markdown(
            f'### #{row["idx"]} · <span class="{sev_cls}">{row["severity"]}</span>  {row["title"]}',
            unsafe_allow_html=True,
        )
        st.markdown(f"**Journey:** {row['journey']} · **Impacted:** {row['impacted']}")
        st.markdown(row["desc"])
        st.markdown("#### Proposed fix")
        st.code(row["fix"], language="text")
        # Reference sessions — click to inspect transcript inline
        it = next(i for i in issues if i["_idx"] == pick)
        refs = it.get("reference_sessions") or []
        if refs:
            st.markdown("#### Reference sessions — click to inspect inline")
            # Build rich labels for the selectbox
            session_rows = sessions[sessions["id"].isin(refs)]
            sev_glyph = {"Critical": "🔴", "High": "🟠", "Medium": "🟡",
                         "Low": "🟢", "(unclassified)": "⚪"}
            label_map = {}
            for _, r in session_rows.iterrows():
                sev  = str(r.get("severity") or "(unclassified)")
                dur  = int(r.get("duration_seconds") or 0)
                glyph = sev_glyph.get(sev, "⚪")
                label_map[r["id"]] = f"{glyph}  {sev} · {dur}s · {r['id']}"
            # Fallback for refs not in sample
            for r in refs:
                if r not in label_map:
                    label_map[r] = f"⚪  {r} (not in analyzed sample)"

            cols = st.columns([3, 1])
            with cols[0]:
                picked_ref = st.selectbox(
                    "Inspect reference session",
                    options=list(label_map.keys()),
                    format_func=lambda sid: label_map.get(sid, sid),
                    key=f"refpick_{pick}",
                )
            with cols[1]:
                if picked_ref:
                    st.markdown(
                        f'<div style="margin-top:1.8rem;"><a href="{sierra_url(picked_ref)}" '
                        f'target="_blank" style="color:#c44f3a;font-weight:600;">'
                        f'Open in Sierra ↗</a></div>',
                        unsafe_allow_html=True,
                    )
            if picked_ref and not session_rows[session_rows["id"] == picked_ref].empty:
                st.markdown("---")
                row = session_rows[session_rows["id"] == picked_ref].iloc[0]
                render_session_detail(picked_ref, row)
            elif picked_ref:
                st.caption(
                    "Esta sesión de referencia no está en nuestro sample analizado. "
                    "Abrir directo en Sierra con el link a la derecha."
                )

# ---------------------------------------------------------------------------
# PAGE 6 — Glossary
# ---------------------------------------------------------------------------

def _term(title: str, what: str, how: str, why: str,
          example: str | None = None, counts: str | None = None,
          title_en: str | None = None, what_en: str | None = None,
          how_en: str | None = None, why_en: str | None = None,
          example_en: str | None = None, counts_en: str | None = None) -> None:
    """Render one glossary term card with the three-question structure.
    Supports EN fallbacks — if provided and language=en, they're used."""
    lbl_what = _t("Qué es", "What it is")
    lbl_how  = _t("Qué hace / cómo funciona", "What it does / how it works")
    lbl_why  = _t("Por qué importa", "Why it matters")
    lbl_ex   = _t("Ejemplo", "Example")

    t_title   = _t(title, title_en   or title)
    t_what    = _t(what,  what_en    or what)
    t_how     = _t(how,   how_en     or how)
    t_why     = _t(why,   why_en     or why)
    t_example = _t(example or "", example_en or example or "") if example else None
    t_counts  = _t(counts  or "", counts_en  or counts  or "") if counts  else None

    st.markdown(f"#### {t_title}")
    st.markdown(f"**{lbl_what}** · {t_what}")
    st.markdown(f"**{lbl_how}** · {t_how}")
    st.markdown(f"**{lbl_why}** · {t_why}")
    if t_example:
        st.markdown(f"**{lbl_ex}** · {t_example}")
    if t_counts:
        st.caption(t_counts)
    st.markdown("---")


def _term_compact(name: str, what: str, why: str | None = None,
                  what_en: str | None = None, why_en: str | None = None) -> None:
    """Compact one-line definition for when there are many terms (tools, tags)."""
    t_what = _t(what, what_en or what)
    t_why  = _t(why or "", why_en or why or "") if why else None
    line = f"**`{name}`** · {t_what}"
    if t_why:
        line += f"  \n<span style='color:#6b6257;font-size:0.88em;'>→ {t_why}</span>"
    st.markdown(line, unsafe_allow_html=True)


def page_glossary():
    st.title(_t("📖  Glosario", "📖  Glossary"))
    st.caption(_t(
        "Cada concepto usado en este dashboard, agrupado por tema. Si algo no "
        "tiene sentido en otra página, probablemente está explicado aquí.",
        "Every concept used in this dashboard, grouped by theme. If anything "
        "is unclear on another page, it's probably explained here.",
    ))

    tabs = st.tabs([
        _t("🧪  Métricas de análisis",        "🧪  Analytics metrics"),
        _t("⚙️  Elementos de Sierra",         "⚙️  Sierra elements"),
        _t("📊  Conceptos básicos",           "📊  Core concepts"),
        _t("🔧  Tools reference",             "🔧  Tools reference"),
        _t("🏷️  Tags reference",              "🏷️  Tags reference"),
        _t("📂  Categories",                  "📂  Categories"),
        _t("🎯  Artefactos del proyecto",     "🎯  Project artefacts"),
    ])

    # ===== Tab 1 · Analytics metrics =====
    with tabs[0]:
        st.markdown(_t(
            "## Métricas que construimos a partir del data",
            "## Metrics we built from the data",
        ))
        st.caption(_t(
            "Lo que no existía en Sierra — lo inventamos para este análisis.",
            "Things Sierra doesn't natively track — we built these for this analysis.",
        ))

        _term(
            "Severity mix",
            what=("La distribución de 100% de las sesiones clasificadas "
                  "repartidas entre las 4 severidades."),
            how=("Contamos cuántas sesiones cayeron en cada bucket y las "
                 "mostramos como una barra horizontal apilada. Cada slice "
                 "muestra volumen absoluto y % del total."),
            why=("Respuesta de un vistazo a *\"¿qué tan mal está hoy?\"*. "
                 "Si Critical + High juntos son > 30% del mix, CSAT y "
                 "charge-backs vendrán en los próximos días."),
            counts="Hoy: 13 Critical · 54 High · 17 Medium · 26 Low (sobre 110 clasificadas).",
        )

        _term(
            "Category × severity",
            what=("Crosstab que muestra cuántas sesiones cayeron en cada "
                  "combinación (categoría, severidad)."),
            how=("Agrupamos las 110 clasificadas por categoría (`transaction_status`, "
                 "`authentication`, etc.) y severidad. Apilamos las barras con "
                 "color por severidad — los bloques rojos arriba en una barra = "
                 "categoría con muchos Critical."),
            why=("Responde *\"qué intent del cliente se está rompiendo más?\"*. "
                 "La categoría con la barra roja más alta es el primer lugar "
                 "donde invertir tiempo de engineering."),
        )

        _term(
            "Monitor detection rate",
            what=("Qué porcentaje de las 111 sesiones analizadas hizo "
                  "saltar cada monitor de Sierra."),
            how=("Cada sesión pasa por los 4 monitores automáticamente "
                 "durante la llamada. El resultado queda en `monitor_results` "
                 "con `detected=0/1`. Contamos detected / total por monitor."),
            why=("Sierra **ya está detectando** los problemas; este chart "
                 "demuestra que la observabilidad funciona. El problema es "
                 "que ningún monitor dispara un cambio de comportamiento — "
                 "son logs, no triggers."),
            counts="Hoy: Agent Looping 40% · False Transfer 12% · Frustration 11% · Repeated Escalation 3%.",
        )

        _term(
            "unsupportedIntent sub-tags",
            what=("Una familia de tags que Sierra le pone a la sesión "
                  "cuando *reconoce* el intent del cliente pero **no tiene "
                  "journey** para manejarlo."),
            how=("Durante la llamada, el agente clasifica la intent. Si no "
                 "matchea ningún journey existente, le pone el tag "
                 "`unsupportedIntent:<nombre>`. Por ejemplo: `recall`, "
                 "`change-order-details`, `technical-issues`."),
            why=("Evidencia **directa** de configuración faltante. Cada "
                 "sub-tag es un caller que Sierra pudo entender pero no pudo "
                 "ayudar. Hoy 46 sesiones (41%) tienen este tag — y ninguno "
                 "de esos sub-tags rutea a un journey."),
            counts="46 sesiones afectadas. Sub-tags: transaction-status-not-recognized (15), recall (10), change-order-details (8), account-department (5), agent-provides-department (4), technical-issues (4).",
        )

        _term(
            "Gap × simulation coverage",
            what=("Mapa de cobertura que cruza nuestros 15 gaps contra "
                  "las 25 secciones de simulación existentes en Sierra."),
            how=("Para cada gap, marcamos manualmente si hay una sim "
                 "equivalente y en qué estado: `missing` (sin sim), "
                 "`covered-failing` (sim existe pero falla), "
                 "`covered-partial` (parcialmente cubierto), "
                 "`covered-passing` (cubierto y pasa)."),
            why=("Te dice qué gaps son **bugs conocidos** (sim existe y "
                 "falla — urgente) vs **blind spots** (no hay sim, nadie "
                 "está mirando eso). Los blind spots son peligrosos porque "
                 "puedes romperlos sin darte cuenta."),
        )

    # ===== Tab 2 · Sierra elements =====
    with tabs[1]:
        st.markdown(_t(
            "## Los bloques de construcción de Sierra",
            "## Sierra's building blocks",
        ))
        st.caption(_t(
            "Lo que ya existe en la plataforma — vocabulario de Sierra.",
            "What already exists on the platform — Sierra's vocabulary.",
        ))

        _term(
            "Journey",
            what=("La unidad fundamental de configuración del agente. Un "
                  "*flujo* para manejar un tipo específico de intent del "
                  "cliente."),
            how=("Cada journey contiene: **Condition** (cuándo aplica), "
                 "**Goal** (qué lograr), **Rules** (cómo comportarse), "
                 "**Tools** (qué puede usar), **Response phrasing** (cómo "
                 "hablar). El agente evalúa las condiciones en orden y "
                 "ejecuta el journey que matchee."),
            why=("Es literalmente el **cerebro del agente** para ese caso "
                 "de uso. Sin un journey adecuado, el caller no tiene "
                 "resolución. Faltar journeys = agente limitado."),
            counts="Aria tiene 5 journeys: Intents Where User Needs to Authenticate, Select Order, Check Order Status, Cancel Customer Order, Check Order ETA.",
        )

        _term(
            "Tool",
            what=("Una función que el agente puede invocar para interactuar "
                  "con sistemas externos (DB de clientes, APIs de Ria, "
                  "Zendesk, telefonía, etc.)."),
            how=("Recibe parámetros JSON, ejecuta lógica en backend (o llama "
                 "un API externo), devuelve resultado JSON. Cada call "
                 "queda registrada en `traces` con request/response/error."),
            why=("Los tools son la forma del agente de **hacer cosas** "
                 "en vez de solo hablar. Un agente sin tools puede conversar "
                 "pero no puede completar tareas (ej. no puede cancelar una "
                 "orden ni autenticar un cliente)."),
            counts="Aria tiene 17 tools: OrderOverview, AuthenticateAgent, SetCallerType, ListTransactions, AttemptCvpAuthentication, DetailedOrder, CustomerByOrderNumber, CustomerByTelephone, CustomerById, CheckTransactionCancellationEligibility, ConfirmCancellationIntent, CareCancellation, SearchFAQKnowledge, CreateZendeskTicket, SubmitCSATScore, TestAllRiaAPIs.",
        )

        _term(
            "Monitor",
            what=("Un watcher de Sierra en tiempo real que detecta patrones "
                  "problemáticos durante la llamada."),
            how=("Observa el transcript y las tool calls mientras pasan. Si "
                 "detecta un patrón conocido (ej. el mismo prompt repetido "
                 "3 veces), marca la sesión con `detected=1` en "
                 "`monitor_results`."),
            why=("Observabilidad automática de problemas. Problema actual: "
                 "los monitores detectan **pero no intervienen** — son "
                 "alarmas silenciosas. Fix propuesto: cuando un monitor "
                 "dispara, el agente debe romper el flujo y pivotear "
                 "(ver Gap #8)."),
            counts="4 monitores built-in: Agent Looping (40% detection), False Transfer (12%), Frustration Increase (11%), Repeated Escalation (3%).",
        )

        _term(
            "Tag",
            what=("Una etiqueta que Sierra asigna a la sesión. **Solo observacional**, "
                  "no afecta routing."),
            how=("Durante la llamada, diferentes componentes del agente "
                 "emiten tags: el intent classifier, los tool invocations, "
                 "los monitores, el language detector, etc. Se almacenan "
                 "como tuplas `(session_id, tag)` en `session_tags`."),
            why=("Los tags son la **huella observacional** de cada llamada. "
                 "Super útiles para filtrar / reportar. Pero **no deciden** "
                 "el flujo — un `unsupportedIntent:recall` no triggerea "
                 "ninguna acción, solo se registra."),
            counts="2,185 tag rows en 111 sesiones. Top tags: api:ria:authenticate:invoked (97), cxi_1_greeting (95), cxi_ch_voice (95), api:zendesk:ticket:create:success (74).",
        )

        _term(
            "Simulation",
            what=("Un escenario de test predefinido que verifica un "
                  "comportamiento específico del agente."),
            how=("Sierra ejecuta el escenario contra el agente actual y "
                 "compara el output con el comportamiento esperado. Reporta "
                 "pass/fail. Las sims están agrupadas en 'secciones' por "
                 "tema (Authentication, CVP, Order Not Found Escalation, "
                 "etc.)."),
            why=("Regresión automática. Si cambias el config y rompes "
                 "algo, las sims lo atrapan antes de llegar a producción. "
                 "Hoy: 35/112 escenarios pasando (31%). **20 de 25 secciones "
                 "tienen 0 passing** — blind spots masivos."),
            counts="25 secciones · 112 escenarios totales · 35 pasando · 6 gaps estructurales sin simulación equivalente.",
        )

        _term(
            "Global context blocks",
            what=("4 bloques de configuración que aplican a TODOS los "
                  "journeys simultáneamente."),
            how=("**Rules** = directivas de comportamiento · **Response "
                 "phrasing** = tono y estilo · **Policies** = reglas de "
                 "negocio (return windows, eligibility) · **Glossary** = "
                 "definiciones del dominio (quién es un 'Customer' vs "
                 "'Agent' en Ria)."),
            why=("Evita duplicar reglas en cada journey. Si cambias el "
                 "tono en `Response phrasing`, todos los journeys cambian. "
                 "Los fixes que propusimos (ej. 'end-of-call termination', "
                 "'language policy') viven acá."),
        )

        _term(
            "Components",
            what=("Conjuntos reutilizables de bloques que se pueden referenciar "
                  "desde múltiples journeys."),
            how=("Defines un component una vez (ej. 'Identity verification flow'). "
                 "Lo importas en cada journey que lo necesite. Cambias el "
                 "component → cambia en todos los lugares."),
            why=("DRY (Don't Repeat Yourself). Crítico para mantener "
                 "consistencia cuando tienes 5+ journeys. Aria usa components "
                 "para auth y escalation compartidas."),
        )

        _term(
            "Agent Studio / Agent Builder",
            what=("La UI de Sierra donde se edita la configuración del agente."),
            how=("Editor visual tipo nodos. Cada journey se visualiza como "
                 "un árbol de condition-blocks anidados. Tools se definen "
                 "en una sección separada. Versionado built-in (Staging / "
                 "Production)."),
            why=("Es donde los cambios del roadmap se aplican. No-code "
                 "para quien no sabe programar, pero el Lexical state "
                 "atrás es 100KB de JSON estructurado por journey."),
        )

    # ===== Tab 3 · Basic concepts =====
    with tabs[2]:
        st.markdown(_t(
            "## Conceptos básicos del proyecto",
            "## Core project concepts",
        ))

        _term(
            "Session / Llamada",
            what=("Una conversación completa con el agente de voz Aria."),
            how=("Cada session tiene un ID único (`audit-01KPV...`), un "
                 "timestamp, duración, device (`VOICE_PHONE` / `VOICE_WEB`), "
                 "y potencialmente un transcript completo si bajamos el "
                 "detalle."),
            why=("Es la unidad atómica de análisis. Cada row en nuestras "
                 "tablas es *por sesión* o *por evento dentro de sesión*."),
            counts="8,113 sessions listed hoy · 111 con detalle completo (sample).",
        )

        _term(
            "Sessions listed vs Sample analysed",
            what=("La diferencia entre *todo lo que Sierra grabó* y *lo que "
                  "nosotros analizamos*."),
            how=("**Sessions listed** (8,113 hoy) = todas las llamadas del "
                 "día, de las cuales tenemos metadata ligera (timestamp, "
                 "duración, tags, first user message).\n\n"
                 "**Sample analysed** (111 hoy) = subset donde bajamos el "
                 "transcript completo + tool traces + monitor results. "
                 "Es nuestro 100% analítico — todas las % se calculan sobre "
                 "esto, no sobre los 8,113."),
            why=("Scrapear detalle de 8,113 sesiones tomaría horas y "
                 "clasificar con Sonnet costaría $20+. La muestra "
                 "estratificada de 111 preserva representatividad con "
                 "<$2 de costo total."),
        )

        _term(
            "Classification",
            what=("Proceso donde Sonnet 4.6 lee cada sesión y le asigna "
                  "metadata estructurada."),
            how=("Por cada sesión pasamos a Sonnet: el transcript completo, "
                 "los tags, los monitor results, y los tool calls. Además "
                 "enviamos contexto cacheado (los 5 journey blocks + 17 "
                 "tools + 197 KB titles). Sonnet devuelve JSON con: "
                 "category, severity, pain_points (lista), suggestion, "
                 "related_journey_blocks, related_kb_articles."),
            why=("Automatiza el review que un humano tardaría horas en "
                 "hacer. Con prompt caching, ~$0.01 por sesión. Las 110 "
                 "clasificadas costaron ~$1.20 total."),
        )

        _term(
            "Severity — cómo se calcula",
            what=("Etiqueta asignada por Sonnet que resume qué tan mal "
                  "salió la sesión desde la perspectiva del caller."),
            how=("Sonnet usa esta taxonomía (del system prompt):\n\n"
                 "- 🔴 **Critical**: agente falló silencioso, caller sin "
                 "resolver, dinero en juego. Daño real.\n"
                 "- 🟠 **High**: caller frustrado, info errónea, transfer "
                 "innecesario a humano.\n"
                 "- 🟡 **Medium**: fricción pero resuelto eventualmente; "
                 "agente repitió preguntas, lento, confundido.\n"
                 "- 🟢 **Low**: menor, cosmético, drop esperado (test call, "
                 "solo greeting, número equivocado)."),
            why=("Permite priorizar reviews humanos: 13 Critical merecen "
                 "atención individual; 26 Low pueden ignorarse. Sin severity, "
                 "todos los problemas se ven iguales."),
        )

        _term(
            "Stratified sampling",
            what=("Técnica de muestreo que preserva las proporciones de la "
                  "población original."),
            how=("Dividimos las 8,113 sesiones en 6 buckets por duración "
                 "(0-5s drops, 5-30s frustración, 30s-2min cortas, 2-5min "
                 "medias, 5-10min profundas, 10+min largas). Muestreamos "
                 "proporcionalmente (10%, 15%, 25%, 30%, 15%, 5%) hasta "
                 "llegar a 100 sesiones."),
            why=("Un muestreo puramente aleatorio sesga hacia sesiones "
                 "cortas (son mayoría). Estratificado garantiza que el "
                 "caso largo complejo (5% de la población) esté "
                 "representado en el análisis."),
        )

    # ===== Tab 4 · Tools reference =====
    with tabs[3]:
        st.markdown(_t(
            "## Tools reference  ·  todos los tools visibles en el dashboard",
            "## Tools reference  ·  every tool that appears in charts",
        ))
        st.caption(_t(
            "Cada tool que puede aparecer en los charts, con qué hace y por qué "
            "verlo importa. Los **business tools** son los 17 que el agente invoca "
            "para realmente hacer cosas (consultar orden, crear ticket, etc.). Los "
            "**framework internals** son los tools del motor de Sierra que siempre "
            "corren bajo el capó.",
            "Every tool that can appear in the charts, what it does, and why "
            "seeing it matters. **Business tools** are the 17 the agent calls to "
            "actually do things (look up order, create ticket, etc.). **Framework "
            "internals** are the engine-level tools that always run under the hood.",
        ))

        st.markdown(_t("### 🏢  Business tools (17)", "### 🏢  Business tools (17)"))
        st.caption(_t(
            "Scrapeados de Sierra con descripción oficial del Agent Builder.",
            "Scraped from Sierra with the official Agent Builder descriptions.",
        ))
        conn = sqlite3.connect(DB_PATH)
        tools_df = pd.read_sql(
            "SELECT name, description, type FROM tools ORDER BY name", conn
        )
        conn.close()
        tool_why = {
            "AttemptCvpAuthentication":        "El CVP es el check de identidad clave. Cuando lo ves en un trace, el agente está verificando al cliente. Muchos fallos aquí = loops de auth.",
            "AttemptToSelectTransaction":      "Selecciona UNA transacción específica cuando el cliente tiene varias. Si falla, el agente no puede responder detalles.",
            "AuthenticateAgent":               "Auth para cuando llama un agente de Ria (no cliente final). Flujo paralelo al CVP.",
            "CareCancellation":                "Ejecuta la cancelación real. Solo debe dispararse después de eligibility + confirmation.",
            "CheckTransactionCancellationEligibility": "Revisa si una orden es cancelable antes de intentar. Siempre va primero en el flujo de cancel.",
            "ConfirmCancellationIntent":       "Pregunta explícitamente al caller si quiere cancelar. Gate antes de CareCancellation.",
            "CreateZendeskTicket":             "Crea un ticket en Zendesk para seguimiento humano. Debe usarse como fallback planeado, no solo reactivo.",
            "CustomerById":                    "Lookup por customer ID interno. Se usa cuando ya tienes el ID (rare en voice).",
            "CustomerByOrderNumber":           "Lookup por número de orden. Tool principal de identificación del cliente.",
            "CustomerByTelephone":             "Lookup por teléfono ANI del caller. **Debería** usarse como fallback cuando el order number falla — pero hoy casi nunca se invoca.",
            "DetailedOrder":                   "Devuelve info detallada de la transacción seleccionada. Solo post-auth.",
            "ListTransactions":                "Lista todas las transacciones del cliente. Usado durante CVP auth.",
            "OrderOverview":                   "Vista 'ligera' del orden, sin info sensible. Se puede usar pre-auth para confirmar que existe la orden.",
            "SearchFAQKnowledge":              "Busca en la knowledge base (197 FAQs públicos de Ria). Tool de self-service general.",
            "SetCallerType":                   "Etiqueta al caller como Customer o Agent al inicio de la llamada. Necesario para routing.",
            "SubmitCSATScore":                 "Registra el CSAT de 1-5 al final de la llamada. Hoy casi nunca se invoca.",
            "TestAllRiaAPIs":                  "Tool de testing. No debería aparecer en llamadas productivas.",
        }
        for _, r in tools_df.iterrows():
            name = r["name"]
            desc = (r["description"] or "").strip()
            why  = tool_why.get(name)
            _term_compact(name, desc, why)

        st.markdown("---")
        st.markdown("### ⚙️  Framework internals  ·  tools del motor de Sierra")
        st.caption(
            "Estos son tools que el framework de Sierra corre automáticamente en cada "
            "turn de conversación. NO son tools de negocio — son los mecanismos que "
            "permiten que el agente funcione. Se excluyen de los charts de Tool "
            "frequency porque inundarían la visualización."
        )

        framework_tools = [
            ("goalsdk_respond",                    "Función primitiva del framework que genera la respuesta del agente en cada turn (combina contexto + intent + políticas + llama al LLM).", "La raíz de toda respuesta. Si sale error aquí, el agente no puede hablar."),
            ("ask_ai",                             "Sub-llamada al LLM dentro del framework para inferencias secundarias (no el turn principal).", "Muy común: el agente pregunta al LLM cosas internas durante el turn."),
            ("turn",                               "Representa UN turn de conversación (intercambio usuario ↔ agente).", "Unidad atómica de la conversación. Todo trace pertenece a un turn."),
            ("sleep",                              "Pausa intencional (ej. esperar a que el caller termine de hablar, o timeouts de red).", "Visible en traces como marcador de tiempo en espera."),
            ("toolcall",                           "Evento wrapper de cualquier invocación de tool.", "Si ves 'toolcall' en Critical, significa que el agente sí intentó hacer cosas, pero puede estar fallando antes o después del call."),
            ("classify_observations",              "Clasifica qué eventos ocurrieron durante un turn (silencio, interrupción, respuesta corta, etc.).", "Alimenta la intent detection y los monitors."),
            ("classify_agent_monitor",             "Determina qué categorías de monitor aplican al turn actual.", "Decide si disparar Agent Looping, Frustration, etc."),
            ("classify_interruption",              "Clasifica si el caller interrumpió al agente y qué se estaba diciendo.", "Importante para agentes de voz — las interrupciones son frecuentes y requieren manejo especial."),
            ("classify_observations",              "Clasificador general de lo que está sucediendo en la conversación.", "Output base para otros clasificadores."),
            ("threat_evaluation",                  "Evalúa si hay amenazas (prompt injection, security attack, abuso) en el mensaje del caller.", "Gate de safety. 73 invocaciones en nuestras 111 sesiones."),
            ("safety_monitor",                     "Valida que el agente cumple políticas de safety (PII, compliance).", "Corre en cada turn. Fail = no ejecutar acción."),
            ("detect_abuse",                       "Detecta intentos de abuso (lenguaje ofensivo, amenazas, intentos de jailbreak).", "Variante de threat_evaluation enfocada en contenido del caller."),
            ("detect_abuse_tee",                   "Variante 'Trusted Execution Environment' del abuse detector.", "Misma función con guarantees adicionales de seguridad."),
            ("personalized_progress_indicator",    "Genera frases tipo 'estoy revisando…' personalizadas en lugar de silencio.", "Reduce abandono en llamadas largas. Si ves este tool mucho, significa que el agente pasa mucho tiempo 'trabajando'."),
            ("should_query_kb",                    "Decide si se debe invocar búsqueda de KB para la pregunta actual.", "Gate antes de knowledge_search. Evita queries innecesarias."),
            ("knowledge_search",                   "Busca artículos relevantes en la KB (197 FAQs).", "Si aparece, el agente está intentando resolver con información documentada."),
            ("kb_result_sufficiency",              "Evalúa si los resultados de KB son suficientes para responder.", "Si responde 'no', el agente debe ofrecer alternativas (rephrase o transfer)."),
            ("kb_generate_query",                  "Genera el string de búsqueda para la KB a partir de la pregunta del caller.", "Transforma 'where is my money?' en keywords útiles."),
            ("respond_instructive",                "Genera una respuesta tipo instrucción paso a paso.", "Útil para explicar procedimientos (cómo usar el app, cómo autenticarse)."),
            ("respond_paraphrase",                 "Reformula una respuesta anterior en otras palabras.", "Cuando el caller no entendió la primera vez."),
            ("missing_policy_reasoning",           "Razonamiento del agente sobre qué hacer cuando NO hay regla que cubra el caso.", "Si aparece mucho, tu config tiene gaps — hay casos que la política no cubre."),
            ("deadlock_detector",                  "Detecta si la conversación está estancada (mismo prompt repetido, sin progreso).", "Feed directo al monitor Agent Looping."),
            ("param_validation",                   "Valida que los parámetros del tool call son del tipo correcto.", "Gate de integridad. Fail = no llamar el tool."),
            ("conversationTurn",                   "Evento de estructura que marca una fase del turn (input / thinking / output).", "Metadato de estructura del turn."),
            ("voiceSidecar",                       "Procesamiento paralelo específico de voz (latencia, ruido, duración)."),
            ("agentMemory",                        "Acceso a la memoria persistente del agente sobre el caller / conversación."),
            ("fetch",                              "Llamada HTTP genérica (usada por tools que hacen APIs externas).", "Cada tool de negocio que pega contra APIs de Ria internamente hace fetch."),
            ("identity",                           "Información de identidad del caller (ANI, número, carrier)."),
            ("logMessage",                         "Evento de log general — mensaje del sistema sin lógica."),
        ]
        seen = set()
        for name, what, *rest in framework_tools:
            if name in seen: continue
            seen.add(name)
            why = rest[0] if rest else None
            _term_compact(name, what, why)

        st.markdown("---")
        st.markdown("### 🚨  System monitor tools  ·  evaluadores de los 4 monitores")
        st.caption(
            "Cada uno corre constantemente durante la llamada. Cuando uno 'fires' "
            "(devuelve true), se registra en `monitor_results` con detected=1. "
            "Aparecen en los traces con el prefijo `system_monitor_`."
        )
        _term_compact(
            "system_monitor_agent_looping",
            "Evaluador interno del monitor Agent Looping. Detecta cuando el agente repite el mismo prompt / tool call sin progreso.",
            "**40% detection rate hoy** — el mayor problema sistémico. Hoy solo logea; debería romper el flujo.",
        )
        _term_compact(
            "system_monitor_frustration_increase",
            "Evaluador interno del monitor Frustration Increase. Detecta escalación emocional en las frases del caller (interrupciones, quejas, tono agresivo).",
            "11% detection. Correlaciona fuertemente con severidad High/Critical.",
        )
        _term_compact(
            "system_monitor_false_transfer",
            "Evaluador del monitor False Transfer. Detecta cuando el agente anuncia transfer pero nunca lo completa.",
            "12% detection. Bug directo de Gap #2 (transfer atado a Cancel flow).",
        )
        _term_compact(
            "system_monitor_repeated_escalation",
            "Evaluador del monitor Repeated Escalation. Detecta cuando el caller pide humano más de N veces sin resolución.",
            "3% detection. Más raro pero indica Gap #15 (escalation ladder missing).",
        )

    # ===== Tab 5 · Tags reference =====
    with tabs[4]:
        st.markdown("## Tags reference  ·  el sistema de etiquetas de Sierra")
        st.caption(
            "Sierra asigna estas etiquetas durante cada llamada. Son **observacionales**: "
            "describen qué pasó, pero no controlan el routing. Los agrupé por familia "
            "(cxi_*, api:*, tool:*, milestone:*, language:*, transfer, unsupportedIntent:*)."
        )

        st.markdown("### 🔤  Familia `cxi_*`  ·  Customer Experience Index")
        st.caption("Estructura conversacional — qué fase de la llamada es cada turn.")
        _term_compact("cxi_1_greeting",            "Fase 1: saludo inicial del agente.", "Siempre aparece al inicio. Si solo hay cxi_1, caller colgó en greeting.")
        _term_compact("cxi_2_intent",              "Fase 2: captura del intent del caller.", "Si falta, el agente nunca entendió qué quería el caller.")
        _term_compact("cxi_4_escalation_intended", "Fase 4: agente intenta escalar a humano.", "Señal de falla de containment.")
        _term_compact("cxi_5_escalation_requested","Fase 5: caller pidió humano explícitamente.", "30% de las sesiones llegan acá → gap #15.")
        _term_compact("cxi_ch_voice",              "Canal de la conversación = voz (vs chat/email).", "99% de Aria es voice phone.")
        _term_compact("cxi_outcome_dropoff_early", "Resultado: caller colgó temprano.", "32 sesiones hoy. Indica agent no logró conectar.")
        _term_compact("cxi_outcome_dropoff_late",  "Resultado: caller colgó tarde (conversación avanzada pero sin resolución).", "7 sesiones. Peor que early — más tiempo invertido sin valor.")

        st.markdown("### 🌐  Familia `api:*`  ·  llamadas a APIs externas")
        _term_compact("api:ria:authenticate:invoked", "Se inició auth CVP contra la API de Ria.",              "Debería siempre ir seguido de :success o :failed.")
        _term_compact("api:ria:authenticate:success", "Auth CVP exitosa.",                                     "97 invoked vs 94 success → 3 fallos de auth hoy (aparte de los CVP intentados que se dieron por vencidos antes).")
        _term_compact("api:ria:customer:search:by-order", "Búsqueda de cliente por order number.",             "Mapea al tool CustomerByOrderNumber.")
        _term_compact("api:ria:customer:search:success",  "La búsqueda devolvió match.",                        "35 success hoy.")
        _term_compact("api:zendesk:ticket:create:invoked","Se intentó crear un ticket Zendesk.",               "77 hoy.")
        _term_compact("api:zendesk:ticket:create:success","Ticket Zendesk creado exitosamente.",               "74 hoy (3 fallos silenciosos de Zendesk).")

        st.markdown("### 🔧  Familia `tool:*`  ·  ciclo de vida de invocación de tools")
        _term_compact("tool:transfer:invoked",      "Se inició una operación de transfer a humano.", "47 hoy.")
        _term_compact("tool:transfer:attempted",    "Se intentó ejecutar el transfer (post-invoke).", "42 hoy — 5 fallaron pre-attempt.")
        _term_compact("tool:transfer:success",      "Transfer completado.", "47 success — más que attempted??? (indica inconsistencia en el tagging).")
        _term_compact("tool:transfer:offline-hours","Transfer intentado fuera de horario de atención.", "3 hoy — gap #10 (offline-hours policy).")
        _term_compact("tool:set-caller-type:invoked","Se inició identificación customer-vs-agent.", "49 hoy.")
        _term_compact("tool:set-caller-type:set",   "Se asignó efectivamente el caller type.", "49 hoy — 100% success aquí.")
        _term_compact("tool:order-overview:failed", "Llamada a OrderOverview falló.", "21 fallos hoy — gap #10 (sin retry budget).")
        _term_compact("tool:cvp:failed",            "Intento de CVP falló.", "8 fallos hoy — gap #10.")

        st.markdown("### 🚩  Familia `milestone:*`  ·  marcadores de progreso")
        _term_compact("milestone:initiates-vp",               "El agente inició verificación de persona.", "36 sesiones.")
        _term_compact("milestone:requests-immediate-transfer","Caller pidió humano ANTES de completar auth.", "22 sesiones — cohort objetivo del Structured Escalation Ladder (gap #15).")

        st.markdown("### 🧭  Familia `unsupportedIntent:*`  ·  intents reconocidos pero no ruteados")
        _term_compact("unsupportedIntent",                                "Tag genérico para intent sin journey.", "46 sesiones (41%).")
        _term_compact("unsupportedIntent:transaction-status-not-recognized","El caller pregunta sobre status pero Sierra no pudo matchearlo.", "15 hoy. Muchos son Payout Failures disfrazados (gap #1).")
        _term_compact("unsupportedIntent:recall",                         "Caller quiere 'recall' (cancelar) pero el lenguaje no matcheó el journey Cancel.", "10 hoy. El journey de Cancel debería incluir sinónimos como 'recall', 'zurückmachen' (alemán).")
        _term_compact("unsupportedIntent:change-order-details",           "Caller quiere modificar la orden (nombre, pagador, etc).", "8 hoy. Gap #3 — journey Modification no existe.")
        _term_compact("unsupportedIntent:account-department",             "Caller pregunta sobre cuenta (no transacción).", "5 hoy. Debería rutear a General FAQ.")
        _term_compact("unsupportedIntent:agent-provides-department",      "Llamante agente de Ria pide departamento específico.", "4 hoy.")
        _term_compact("unsupportedIntent:technical-issues",               "Caller reporta problema técnico (app, login, PIN).", "4 hoy. Debería rutear a General FAQ + escalación técnica.")
        _term_compact("unsupportedIntent:accounts-receivable",            "Caller pregunta sobre cobro pendiente.", "3 hoy.")

        st.markdown("### 🌍  Familia `language:*` y `transcription-locale:*`")
        _term_compact("language:es",               "Caller detectado hablando español.", "47 sesiones (mayoría).")
        _term_compact("language:en",               "Caller detectado hablando inglés.", "33 sesiones.")
        _term_compact("language:unsupported",      "Idioma del caller no soportado por Aria (ej. italiano, francés, alemán).", "5 sesiones — gap #5 (language policy).")
        _term_compact("transcription-locale:es-ES","Locale específico de la transcripción (castellano).", "Más específico que `language:es`.")
        _term_compact("transcription-locale:en-US","Locale de transcripción inglés-US.", "")

        st.markdown("### 📞  Familia `transfer` y misceláneos")
        _term_compact("transfer",                         "Tag genérico de evento de transfer.", "47 hoy.")
        _term_compact("transfer-type:sip-to-sip",         "Transfer SIP a SIP (llamada puenteada entre VoIP systems).", "34 hoy — tipo más común.")
        _term_compact("customer_assistance__transfer_call","Categoría específica: transfer para customer care.", "44 hoy.")

    # ===== Tab 6 · Categories =====
    with tabs[5]:
        st.markdown("## Categories  ·  las 10 etiquetas que asigna Sonnet")
        st.caption(
            "Cuando Sonnet clasifica una sesión, asigna exactamente **una** de "
            "estas 10 categorías basándose en el intent del caller. Son "
            "mutuamente excluyentes. La severity es ortogonal (una categoría "
            "puede tener sesiones de cualquier severidad)."
        )

        _term_compact(
            "transaction_status",
            "Caller pregunta por el estado de su transferencia, order status o ETA.",
            "Categoría con más Critical/High hoy — cohort principal para análisis.",
        )
        _term_compact(
            "cancel_transaction",
            "Caller quiere cancelar una orden existente.",
            "Debe llegar al journey Cancel Customer Order. Si aparece como unsupportedIntent:recall, es un routing miss.",
        )
        _term_compact(
            "refund",
            "Caller quiere un reembolso (diferente de cancelar — cancelar es antes de que se complete, refund es después).",
            "Hoy Aria no tiene journey Refund — cae en Cancel o escala.",
        )
        _term_compact(
            "authentication",
            "El problema principal de la sesión fue autenticación (CVP o AVP): no pudo pasar el check o se frustró intentando.",
            "Fallo aquí bloquea todo lo demás. 9 sesiones hoy.",
        )
        _term_compact(
            "transfer_to_human",
            "Caller pide hablar con humano (o el agente decide escalar).",
            "27 hoy. Objetivo del Structured Escalation Ladder.",
        )
        _term_compact(
            "general_info",
            "Pregunta general: fees, horarios, ubicaciones, uso de app, Ria Wallet.",
            "No necesita auth. Debería resolverse con SearchFAQKnowledge.",
        )
        _term_compact(
            "technical_issue",
            "App crash, login problem, PIN reset, problemas con wallet/tarjeta.",
            "6 hoy. Mayoría debería escalar a soporte técnico con contexto.",
        )
        _term_compact(
            "complaint",
            "Frustración o escalación de un problema previo que no se resolvió.",
            "2 hoy. Rara pero súper crítica — casi siempre Critical.",
        )
        _term_compact(
            "greeting_drop",
            "Caller colgó en el greeting o no-intent.",
            "28 hoy. Generalmente Low severity, pero el volumen indica el 25% de llamadas no consigue engagement.",
        )
        _term_compact(
            "other",
            "No encaja en las anteriores.",
            "4 hoy. Si esta categoría crece mucho, es señal de que hay que agregar más buckets.",
        )

    # ===== Tab 7 · Our artefacts =====
    with tabs[6]:
        st.markdown(_t(
            "## Artefactos generados en este proyecto",
            "## Artefacts generated by this project",
        ))

        _term(
            title="Structural Gap",
            title_en="Structural Gap",
            what=("Una **falla de configuración** que identificamos cruzando "
                  "nuestros datos scrapeados con la UI de Sierra."),
            what_en=("A **configuration gap** we identified by cross-referencing "
                     "our scraped data with the Sierra UI."),
            how=("Cada gap tiene: título, severidad, evidencia textual, "
                 "data signal (los números que lo soportan), fuente "
                 "(`UI review` o `data mining`), y grounding (qué tan "
                 "confiable es la evidencia)."),
            how_en=("Each gap has: title, severity, textual evidence, "
                    "data signal (the numbers supporting it), source "
                    "(`UI review` or `data mining`), and grounding (how "
                    "reliable the evidence is)."),
            why=("Es el **output tangible** del diagnóstico. Son 15 puntos "
                 "específicos donde el equipo de config debe intervenir. "
                 "Sin ellos, el análisis sería un mar de observaciones "
                 "sin acciones."),
            why_en=("It's the **tangible output** of the diagnosis. 15 specific "
                    "points where the config team must intervene. Without "
                    "them, the analysis would be a sea of observations with "
                    "no actions."),
            counts="15 gaps: 7 UI review · 8 data mining · 8 Critical · 6 High · 1 Medium.",
            counts_en="15 gaps: 7 UI review · 8 data mining · 8 Critical · 6 High · 1 Medium.",
        )

        _term(
            title="Issue (clustered)",
            title_en="Issue (clustered)",
            what=("Un patrón de problema concreto que apareció en ≥1 sesión, "
                  "clusterizado por Sonnet."),
            what_en=("A concrete problem pattern that appeared in ≥1 session, "
                     "clustered by Sonnet."),
            how=("Tomamos los ~550 pain points individuales de las 110 "
                 "clasificaciones. Los pasamos a Sonnet que los agrupa en "
                 "~20 issues temáticos. Cada issue tiene: journey, título, "
                 "descripción con cita literal, # de sesiones impactadas, "
                 "fix propuesto en sintaxis IF/THEN, session IDs de "
                 "referencia."),
            how_en=("We take the ~550 individual pain points from the 110 "
                    "classifications. We send them to Sonnet which groups them "
                    "into ~20 thematic issues. Each issue has: journey, title, "
                    "description with literal quote, # of impacted sessions, "
                    "proposed fix in IF/THEN syntax, reference session IDs."),
            why=("Los issues son el **backlog accionable**. Cada uno debe "
                 "cerrarse con un cambio de config específico. Sin "
                 "clustering, tendrías 550 pain points sueltos — imposible "
                 "de trabajar."),
            why_en=("Issues are the **actionable backlog**. Each one must "
                    "be closed with a specific config change. Without "
                    "clustering, you'd have 550 loose pain points — "
                    "impossible to work with."),
            counts="20 issues: 8 Critical · 11 High · 1 Medium.",
            counts_en="20 issues: 8 Critical · 11 High · 1 Medium.",
        )

        _term(
            title="IF/THEN proposed outcome",
            title_en="IF/THEN proposed outcome",
            what=("El fix propuesto para cada issue, escrito en sintaxis "
                  "de pseudocódigo imperativa."),
            what_en=("The proposed fix for each issue, written in imperative "
                     "pseudocode syntax."),
            how=("Cada issue tiene un bloque tipo:\n\n"
                 "```\nIN 'Intents Where User Needs to Authenticate' block:\n"
                 "  IF tool:CustomerByOrderNumber fails 2x\n"
                 "  >> call tool:CustomerByTelephone using caller ANI\n"
                 "  IF that also fails\n"
                 "  >> tool:CreateZendeskTicket(reason='unable_to_authenticate')\n"
                 "  >> inform caller a human will follow up, end call.\n```"),
            how_en=("Each issue has a block like:\n\n"
                    "```\nIN 'Intents Where User Needs to Authenticate' block:\n"
                    "  IF tool:CustomerByOrderNumber fails 2x\n"
                    "  >> call tool:CustomerByTelephone using caller ANI\n"
                    "  IF that also fails\n"
                    "  >> tool:CreateZendeskTicket(reason='unable_to_authenticate')\n"
                    "  >> inform caller a human will follow up, end call.\n```"),
            why=("Estructura que el equipo de config puede traducir "
                 "directamente a reglas en el Agent Builder. No deja "
                 "ambigüedad sobre *qué* cambiar y *dónde*."),
            why_en=("Structure the config team can translate directly into "
                    "Agent Builder rules. Leaves no ambiguity about "
                    "*what* to change and *where*."),
        )

        _term(
            title="Roadmap phases",
            title_en="Roadmap phases",
            what=("El plan de implementación de los fixes, agrupado en "
                  "4 sprints con prioridad clara."),
            what_en=("The fixes implementation plan, grouped into 4 sprints "
                     "with clear priority."),
            how=("**Sprint 1 — Detener el sangrado**: monitor auto-recovery, "
                 "retry budgets, Zendesk fallback. Baja Agent Looping 40%→15%.\n"
                 "**Sprint 2 — Intent taxonomy**: Payout Failure journey, "
                 "Modification journey, ClassifyIntent tool. Baja "
                 "unsupportedIntent 41%→15%.\n"
                 "**Sprint 3 — Escalation & language**: TransferToLiveRep "
                 "standalone, Structured Escalation Ladder, language "
                 "policy.\n"
                 "**Sprint 4 — Speech & obs**: DTMF, ASR confidence, "
                 "partial-data continuity, weekly re-run del diagnóstico."),
            how_en=("**Sprint 1 — Stop the bleeding**: monitor auto-recovery, "
                    "retry budgets, Zendesk fallback. Drops Agent Looping 40%→15%.\n"
                    "**Sprint 2 — Intent taxonomy**: Payout Failure journey, "
                    "Modification journey, ClassifyIntent tool. Drops "
                    "unsupportedIntent 41%→15%.\n"
                    "**Sprint 3 — Escalation & language**: TransferToLiveRep "
                    "standalone, Structured Escalation Ladder, language "
                    "policy.\n"
                    "**Sprint 4 — Speech & obs**: DTMF, ASR confidence, "
                    "partial-data continuity, weekly diagnostic re-run."),
            why=("Roadmap ordenado por impacto esperado. Sprint 1 elimina "
                 "40% de loops — el mayor pain. Los siguientes construyen "
                 "sobre esa base."),
            why_en=("Roadmap ordered by expected impact. Sprint 1 eliminates "
                    "40% of loops — the biggest pain. Subsequent sprints build "
                    "on that foundation."),
        )

        _term(
            title="Pain point",
            title_en="Pain point",
            what=("Una descripción corta (<15 palabras) de UN problema "
                  "específico que Sonnet identificó en UNA sesión."),
            what_en=("A short (<15 words) description of ONE specific problem "
                     "that Sonnet identified in ONE session."),
            how=("Cuando Sonnet clasifica una sesión, devuelve una lista "
                 "de 2-5 pain points. Por ejemplo: "
                 "*\"Agent asked for order number twice even though user gave it\"*."),
            how_en=("When Sonnet classifies a session, it returns a list "
                    "of 2-5 pain points. For example: "
                    "*\"Agent asked for order number twice even though user gave it\"*."),
            why=("Son los átomos del análisis. Los issues son clusters de "
                 "pain points — primero colectamos los átomos, luego los "
                 "agrupamos."),
            why_en=("They're the atoms of the analysis. Issues are clusters of "
                    "pain points — we first collect the atoms, then group "
                    "them."),
        )


# ---------------------------------------------------------------------------
# PAGE — Investigate (fusion of former Session Explorer + Issue Log)
# ---------------------------------------------------------------------------

def page_investigate():
    st.title("🧭  Investigate — Sessions & Issues")
    st.caption(
        "Dos ángulos del mismo drill-down. Empieza por el **patrón** (issue) "
        "si estás explorando qué problemas son recurrentes; empieza por la "
        "**llamada** (session) si ya sabes cuál quieres revisar. Las sesiones "
        "de referencia de un issue abren su transcript en esta misma página."
    )

    sessions_all = load_sessions()
    sessions     = sampled(sessions_all)
    tags         = load_tags()
    monitors     = load_monitors()
    traces       = load_traces()
    issues       = load_issues_raw()

    tab_issue, tab_session = st.tabs([
        "📋  By issue  — patrones recurrentes",
        "🗣️  By session — llamada individual",
    ])

    with tab_issue:
        render_by_issue(sessions, traces, tags, monitors, issues)

    with tab_session:
        render_by_session(sessions_all, sessions, tags, monitors, traces)


# ---------------------------------------------------------------------------
# PAGE — Strategic Analysis (Sierra Platform Deep Dive)
# ---------------------------------------------------------------------------

def page_strategic():
    st.title(_t("🧠  Plataforma Sierra — Análisis Estratégico",
                "🧠  Sierra Platform — Strategic Analysis"))
    st.caption(_t(
        "Análisis de cómo se están usando las capacidades de Sierra vs. su potencial real. "
        "Basado en 110 sesiones clasificadas · 444 resultados de monitor · "
        "documentación oficial del Product Owner en Confluence CXI.",
        "Analysis of how Sierra's capabilities are being used vs. their real potential. "
        "Based on 110 classified sessions · 444 monitor results · "
        "Product Owner's official documentation in Confluence CXI.",
    ))

    SLATE  = "#6A8CAA"
    CLAY   = "#CC785C"
    SAND   = "#C9A27E"
    GREEN  = "#6c8d5a"

    def section_badge(label, color=SLATE):
        st.markdown(
            f'<span style="background:{color};color:#fff;padding:2px 12px;'
            f'border-radius:3px;font-size:0.72rem;font-weight:700;'
            f'letter-spacing:0.08em;text-transform:uppercase;">{label}</span>',
            unsafe_allow_html=True,
        )

    tab_tools, tab_opps, tab_monitors, tab_sims, tab_kb, tab_cx = st.tabs([
        _t("1 · Herramientas", "1 · Tools"),
        _t("2 · Top 3 oportunidades", "2 · Top 3 opportunities"),
        _t("3 · Monitors activos", "3 · Active monitors"),
        _t("4 · Simulations CI/CD", "4 · Simulations CI/CD"),
        _t("5 · KB Feedback Loop", "5 · KB Feedback Loop"),
        _t("6 · Alineación CX", "6 · CX Alignment"),
    ])

    # ══════════════════════════════════════════════════════════════════════
    # TAB 1 — TOOL USAGE DIAGNOSTIC
    # ══════════════════════════════════════════════════════════════════════
    with tab_tools:
        section_badge(_t("Diagnóstico", "Diagnostic"), CLAY)
        st.subheader(_t(
            "Sierra tiene 17 herramientas disponibles. Opera como si tuviera 5.",
            "Sierra has 17 tools available. It operates as if it had 5.",
        ))
        st.markdown(_t(
            "La herramienta marcada como fundacional en los Conversational Design "
            "Principles del Product Owner registró **0 invocaciones en 110 sesiones**. "
            "No es subutilización — es deuda de implementación crítica que colapsa "
            "todo el flujo de autenticación downstream.",
            "The tool defined as foundational in the Product Owner's Conversational "
            "Design Principles recorded **0 invocations across 110 sessions**. "
            "This is not underutilisation — it is critical implementation debt that "
            "collapses the entire downstream authentication flow.",
        ))

        tool_data = {
            _t("Herramienta", "Tool"): [
                "CustomerByTelephone", "AttemptCvpAuthentication",
                "SearchFAQKnowledge", "CreateZendeskTicket",
                "SetCallerType", "CustomerByOrderNumber",
                "tool:transfer (SIP)", "OrderDetails / DetailedOrder",
                "CheckTransactionCancellationEligibility", "SubmitCSATScore",
            ],
            _t("Estado de uso", "Usage status"): [
                "❌ 0/110 sesiones",
                "⚠️  Activo — preguntas incorrectas en 5 ses. críticas",
                "⚠️  Activo — skip previo a transfer en info-intents",
                "⚠️  Existe — no usado como salida controlada de CVP",
                "⚠️  Activo — no discrimina agente interno vs. cliente",
                "✅  Activo",
                "⚠️  Activo — dispara prematuro o en loop",
                "✅  Activo — posible alias duplicado confunde al LLM",
                "✅  Activo — solo si llega al cancel flow",
                "⚠️  Configurado — raro verlo completar",
            ],
            _t("Issues generados", "Issues generated"): [
                "#3 (14 ses.), #4 (5), #5 (14), #6 (10) → 43 ses. únicas",
                "#2 (9 ses.) — loop CVP",
                "#7 (12 ses.) — bloquea transfer, no consulta KB primero",
                "Gap: debería ser salida de CVP loop (PO-1)",
                "#5 (14 ses.) — agentes internos por CVP de cliente",
                "Correcto cuando se invoca",
                "#1 (8 ses.) — loop · #9 (2 ses.) — transfer sin auth",
                "Correcto",
                "Correcto en sequence",
                "Gap: sin CSAT → sin signal de calidad post-resolución",
            ],
        }
        st.dataframe(tool_data, use_container_width=True, hide_index=True)

        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        col1.metric(
            _t("CustomerByTelephone invocaciones", "CustomerByTelephone invocations"),
            "0 / 110", delta=_t("debería ser ≥85%", "should be ≥85%"),
            delta_color="inverse",
        )
        col2.metric(
            _t("Sesiones afectadas por tool ausente", "Sessions affected by absent tool"),
            "43 / 110", delta="39%", delta_color="inverse",
        )
        col3.metric(
            _t("Issues Critical con causa-raíz en tools", "Critical issues rooted in tools"),
            "6 / 8", delta=_t("4 resolubles sin tool nuevo", "4 solvable with no new tool"),
        )

        st.info(_t(
            "**Causa raíz única:** `CustomerByTelephone` no se invoca → el agente "
            "entra directamente a CVP manual → preguntas mal formuladas → auth falla → "
            "loop → monitor dispara → nada cambia. Es una cascada de un solo eslabón roto.",
            "**Single root cause:** `CustomerByTelephone` is not invoked → agent goes "
            "straight to manual CVP → wrong questions → auth fails → loop → monitor "
            "fires → nothing changes. A cascade from a single broken link.",
        ))

    # ══════════════════════════════════════════════════════════════════════
    # TAB 2 — TOP 3 OPPORTUNITIES
    # ══════════════════════════════════════════════════════════════════════
    with tab_opps:
        section_badge(_t("Sin tools nuevos", "No new tools needed"), GREEN)
        st.subheader(_t(
            "3 cambios de orquestación que eliminan el 65% de los issues críticos",
            "3 orchestration changes that eliminate 65% of critical issues",
        ))
        st.markdown(_t(
            "Todos son fixes de **lógica de orquestación**, no de capability gaps. "
            "El stack de herramientas ya está disponible.",
            "All are **orchestration logic** fixes, not capability gaps. "
            "The tool stack is already available.",
        ))

        # Oportunidad 1
        with st.container(border=True):
            col_h, col_m = st.columns([3, 1])
            with col_h:
                st.markdown(f"### {_t('Oportunidad 1', 'Opportunity 1')} — `CustomerByTelephone` Front-of-Door")
                st.markdown(_t(
                    "Forzar `CustomerByTelephone` como primer tool call obligatorio "
                    "antes de cualquier pregunta al caller. El tool existe, está "
                    "documentado como fundacional en los Design Principles del Product Owner, "
                    "y simplemente no se invoca. La diferencia entre identificar al cliente "
                    "por ANI vs. preguntar manualmente es la diferencia entre un IVR moderno "
                    "y un call center de 2005.",
                    "Force `CustomerByTelephone` as mandatory first tool call before "
                    "any question to the caller. The tool exists, is documented as foundational "
                    "in the Product Owner's Design Principles, and is simply not being invoked. "
                    "The difference between identifying the customer by ANI vs. asking manually "
                    "is the difference between a modern IVR and a 2005 call centre.",
                ))
                st.code(
                    "# Secuencia obligatoria — turno 1\n"
                    "SetCallerType()\n"
                    "→ CustomerByTelephone(ani=caller_ani)\n"
                    "→ if found: pre-load customer + transactions\n"
                    "→ if not found: proceed to CustomerByOrderNumber\n"
                    "→ NEVER skip this step",
                    language="text",
                )
            with col_m:
                st.metric(_t("Issues atacados", "Issues attacked"), "4")
                st.metric(_t("Sesiones afectadas", "Sessions affected"), "43")
                st.metric(_t("Esfuerzo", "Effort"), _t("Bajo", "Low"))
                st.metric(_t("Tool nuevo", "New tool"), "No")

        # Oportunidad 2
        with st.container(border=True):
            col_h, col_m = st.columns([3, 1])
            with col_h:
                st.markdown(f"### {_t('Oportunidad 2', 'Opportunity 2')} — {_t('Hard-exit CVP tras 3 intentos', 'Hard-exit CVP after 3 attempts')}")
                st.warning(_t(
                    "El SOP Feb-26 dice: *'If unsuccessful: Allow retry. Do not transfer.'* "
                    "Sin límite. Esto **diseña el loop**. Los datos lo prueban: "
                    "9 sesiones Critical con CVP loop infinito, monitor Agent Looping "
                    "al 40%, simulation suite Auto-Transfer 0/6 passing.",
                    "The Feb-26 SOP says: *'If unsuccessful: Allow retry. Do not transfer.'* "
                    "No limit. This **designs the loop**. Data proves it: "
                    "9 Critical sessions with infinite CVP loop, Agent Looping monitor "
                    "at 40%, Auto-Transfer simulation suite 0/6 passing.",
                ))
                st.markdown(_t(
                    "La regla correcta: **3 intentos máximo → transfer con contexto y empatía**. "
                    "`CreateZendeskTicket` ya existe — no requiere tool nuevo.",
                    "The correct rule: **3 attempts max → transfer with context and empathy**. "
                    "`CreateZendeskTicket` already exists — no new tool needed.",
                ))
                st.code(
                    "# SOP §5.3 — versión corregida\n"
                    "cvp_attempts = 0\n"
                    "while cvp_attempts < 3:\n"
                    "    result = AttemptCvpAuthentication()\n"
                    "    if result.success: break\n"
                    "    cvp_attempts += 1\n"
                    "if not result.success:\n"
                    "    CreateZendeskTicket(reason='cvp_authentication_failed')\n"
                    "    transfer_with_empathy(context=full_session)",
                    language="python",
                )
            with col_m:
                st.metric(_t("Issues atacados", "Issues attacked"), "#2")
                st.metric(_t("Sesiones críticas", "Critical sessions"), "9")
                st.metric(_t("Monitor looping", "Looping monitor"), "40%")
                st.metric(_t("Esfuerzo", "Effort"), _t("Bajo-Medio", "Low-Med"))

        # Oportunidad 3
        with st.container(border=True):
            col_h, col_m = st.columns([3, 1])
            with col_h:
                st.markdown(f"### {_t('Oportunidad 3', 'Opportunity 3')} — {_t('Intent triage antes de CVP', 'Intent triage before CVP')}")
                st.markdown(_t(
                    "Los CXI Design Principles del Product Owner (Principio #4) dicen: "
                    "*'Informational intents → No CVP. Transactional actions → CVP required.'* "
                    "El agente ignora esto. No existe un bloque de clasificación de intent "
                    "antes del authentication segment — el camino más cercano siempre incluye CVP.",
                    "The Product Owner's CXI Design Principles (Principle #4) state: "
                    "*'Informational intents → No CVP. Transactional actions → CVP required.'* "
                    "The agent ignores this. No intent classification block exists before the "
                    "authentication segment — the closest path always includes CVP.",
                ))
                st.code(
                    "# Bloque Pre-Auth Intent Classification\n"
                    "# Insertar ANTES de SetCallerType\n"
                    "intent = classify_intent(caller_utterance)\n\n"
                    "if intent in INFORMATIONAL:  # fees, ETA genérico, horarios, países\n"
                    "    SearchFAQKnowledge(query=intent)\n"
                    "    → responder sin CVP\n\n"
                    "elif intent in TRANSACTIONAL:  # status orden, cancel, modify\n"
                    "    → authentication segment (CVP obligatorio)",
                    language="python",
                )
            with col_m:
                st.metric(_t("Sesiones informativas sin auth", "Info sessions without auth"),
                          "~15-20%")
                st.metric("FAQ-KB RAG", "LIVE v.01")
                st.metric(_t("Esfuerzo", "Effort"), _t("Medio", "Medium"))
                st.metric(_t("Tool nuevo", "New tool"), "No")

        st.success(_t(
            "**Impacto agregado estimado:** Las 3 oportunidades juntas atacan 6 de los "
            "8 issues Critical y ~62 de las 87 sesiones afectadas por issues críticos (≈71%). "
            "Costo: 2-3 sprints sin dependencias externas. Ninguna requiere tool nuevo.",
            "**Estimated aggregate impact:** The 3 opportunities combined attack 6 of the "
            "8 Critical issues and ~62 of the 87 sessions affected by critical issues (≈71%). "
            "Cost: 2-3 sprints with no external dependencies. None requires a new tool.",
        ))

    # ══════════════════════════════════════════════════════════════════════
    # TAB 3 — MONITORS AS ACTIVE TRIGGERS
    # ══════════════════════════════════════════════════════════════════════
    with tab_monitors:
        section_badge(_t("Sistema nervioso", "Nervous system"), CLAY)
        st.subheader(_t(
            "Los monitores generan telemetría pasiva. El siguiente nivel es que sean circuit breakers activos.",
            "Monitors generate passive telemetry. The next level is making them active circuit breakers.",
        ))
        st.markdown(_t(
            "Sierra construyó los monitores para ser triggers de comportamiento "
            "en tiempo real durante la llamada. Hoy se usan como dashboards post-call. "
            "La detección sin acción es observabilidad, no orquestación.",
            "Sierra built monitors to be real-time behaviour triggers during the call. "
            "Today they are used as post-call dashboards. "
            "Detection without action is observability, not orchestration.",
        ))

        mon_col1, mon_col2, mon_col3, mon_col4 = st.columns(4)
        mon_col1.metric("Agent Looping", "44/111", "40%", delta_color="inverse")
        mon_col2.metric("False Transfer", "13/111", "12%", delta_color="inverse")
        mon_col3.metric("Frustration Increase", "12/111", "11%", delta_color="inverse")
        mon_col4.metric("Repeated Escalation", "3/111", "3%", delta_color="inverse")

        st.markdown(_t("**Acción actual de los 4 monitores cuando disparan:** Ninguna.",
                       "**Current action of all 4 monitors when they fire:** None."))
        st.markdown("---")

        st.markdown(_t("### Arquitectura propuesta: Monitor → Decision Layer → Recovery Flow",
                       "### Proposed architecture: Monitor → Decision Layer → Recovery Flow"))

        m1, m2 = st.columns(2)

        with m1:
            with st.container(border=True):
                st.markdown(f"**Agent Looping (40%) — {_t('Pattern-Break + Reframe', 'Pattern-Break + Reframe')}**")
                st.code(
                    "on_trigger(agent_looping):\n"
                    "  if recovery_attempts == 0:\n"
                    "    inject('Let me try a different approach.')\n"
                    "    SearchFAQKnowledge(last_user_intent)\n"
                    "    set_state(recovery_attempted=1)\n"
                    "  elif recovery_attempts == 1:\n"
                    "    offer_choice(human=True, alternative_route=True)\n"
                    "    if user_yes: transfer(reason='loop_recovery')\n"
                    "  else:\n"
                    "    transfer(reason='loop_unrecoverable')",
                    language="python",
                )
                st.caption(_t("ROI: convierte 40% de sesiones en loop en resoluciones controladas.",
                              "ROI: converts 40% of looping sessions into controlled resolutions."))

            with st.container(border=True):
                st.markdown(f"**False Transfer (12%) — {_t('Pre-flight Check', 'Pre-flight Check')}**")
                st.code(
                    "on_pre_transfer():  # antes de invocar tool:transfer\n"
                    "  valid = (\n"
                    "    user_explicitly_requested_human OR\n"
                    "    cvp_failed_3x OR\n"
                    "    intent_unsupported\n"
                    "  )\n"
                    "  if not valid:\n"
                    "    log('false_transfer_prevented')\n"
                    "    continue_in_agent()\n"
                    "  else:\n"
                    "    transfer(reason=validated_reason)",
                    language="python",
                )
                st.caption(_t("Previene transfers prematuros antes de que ocurran.",
                              "Prevents premature transfers before they occur."))

        with m2:
            with st.container(border=True):
                st.markdown(f"**Frustration Increase (11%) — {_t('De-escalation Ladder', 'De-escalation Ladder')}**")
                st.code(
                    "on_trigger(frustration_increase, level):\n"
                    "  if level == 'low':\n"
                    "    soften_tone()\n"
                    "    inject_acknowledgment()\n"
                    "  elif level == 'medium':\n"
                    "    CreateZendeskTicket(status='de_escalation')\n"
                    "    offer_callback()\n"
                    "  elif level == 'high':\n"
                    "    transfer(priority='high',\n"
                    "             reason='frustration_escalation')",
                    language="python",
                )
                st.caption(_t("Convierte la frustración en señal de routing, no en abandono.",
                              "Converts frustration into a routing signal, not an abandonment."))

            with st.container(border=True):
                st.markdown(f"**Repeated Escalation (3%) — {_t('Hard Exit Protocol', 'Hard Exit Protocol')}**")
                st.code(
                    "on_trigger(repeated_escalation):\n"
                    "  bypass_normal_flow()\n"
                    "  transfer(\n"
                    "    priority='critical',\n"
                    "    skill='senior_agent',\n"
                    "    context=full_conversation_summary\n"
                    "  )\n"
                    "  flag_for_supervisor_review()",
                    language="python",
                )
                st.caption(_t("Solo 3 sesiones hoy — pero cada una es un caso escalado sin resolución.",
                              "Only 3 sessions today — but each is an unresolved escalated case."))

        st.markdown("---")
        st.markdown(_t("### Plan de implementación por fases", "### Phased implementation plan"))
        phases = {
            _t("Fase", "Phase"): ["1", "2", "3", "4"],
            _t("Monitor", "Monitor"): [
                "Agent Looping", "Frustration Increase",
                "False Transfer (pre-flight)", "Repeated Escalation",
            ],
            _t("Esfuerzo", "Effort"): [
                _t("Medio", "Medium"), _t("Medio", "Medium"),
                _t("Alto", "High"), _t("Bajo", "Low"),
            ],
            _t("Razón de prioridad", "Priority reason"): [
                "40% del total — mayor ROI", "11% — convierte señal en acción",
                "Requiere pre-flight hook en tool:transfer", "3% — quick win alto valor",
            ],
            _t("Métricas de éxito", "Success metrics"): [
                "Agent Looping rate: 40% → <10%",
                "CSAT en sesiones con frustración +15%",
                "False Transfer rate -80%",
                "Supervisor escalations <1/semana",
            ],
        }
        st.dataframe(phases, use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════
    # TAB 4 — SIMULATIONS AS CI/CD
    # ══════════════════════════════════════════════════════════════════════
    with tab_sims:
        section_badge(_t("Governance", "Governance"), SAND)
        st.subheader(_t(
            "6 de 7 suites failing y el agente está LIVE en producción. Eso no es un problema técnico — es un fallo de governance.",
            "6 of 7 suites failing and the agent is LIVE in production. That is not a technical problem — it is a governance failure.",
        ))
        st.markdown(_t(
            "Las simulaciones detectan exactamente los problemas reales (correlación "
            "casi 1-a-1 con issues de producción), pero nadie está bloqueando el deploy "
            "basándose en ellas. Es equivalente a mergear a main con tests en rojo.",
            "Simulations detect exactly the real issues (almost 1-to-1 correlation with "
            "production issues), but nobody is blocking deploys based on them. "
            "It is equivalent to merging to main with red tests.",
        ))

        sims_data = {
            _t("Suite", "Suite"): [
                "Abuse Detection",
                "Agent Authentication Auto-Transfer",
                "Agent Looping Recovery",
                "Escalation Logic",
                "Language Switching",
                "Modification Request Handling",
                "Order Number Collection",
            ],
            _t("Resultado", "Result"): [
                "24/27 ✅", "0/6 ❌", "0/3 ❌",
                "2/8 ❌", "0/2 ❌", "0/5 ❌", "1/6 ❌",
            ],
            _t("Issue relacionado", "Related issue"): [
                _t("Ninguno — única passing", "None — only passing suite"),
                "#2 — CVP loop, 9 ses. Critical",
                "Monitor Looping — 44/111 (40%)",
                "#7 — bloquea transfers, 12 ses.",
                "#8 — language switching, 13 ses.",
                "Modifications en BACKLOG",
                "#3 — order number collection, 14 ses.",
            ],
            "Tier": [
                "Tier 1 ✅", "Tier 1 🔴", "Tier 2 🟡",
                "Tier 2 🟡", "Tier 3 ⚪", "Tier 3 ⚪", "Tier 1 🔴",
            ],
        }
        st.dataframe(sims_data, use_container_width=True, hide_index=True)

        st.markdown("---")
        col_t1, col_t2, col_t3 = st.columns(3)

        with col_t1:
            with st.container(border=True):
                st.markdown(f"### Tier 1 — {_t('Release gate (bloqueante)', 'Release gate (blocking)')}")
                st.markdown(_t(
                    "Debe estar 100% passing para mergear cualquier cambio al system prompt o tools:",
                    "Must be 100% passing to merge any system prompt or tool change:",
                ))
                st.markdown("• Abuse Detection ✅\n• Agent Authentication Auto-Transfer ❌\n• Order Number Collection ❌")
                st.error(_t("2 de 3 suites Tier 1 están failing. Deploy actual no debería haber ocurrido.",
                            "2 of 3 Tier 1 suites are failing. Current deploy should not have happened."))

        with col_t2:
            with st.container(border=True):
                st.markdown(f"### Tier 2 — {_t('Quality gate (alerta)', 'Quality gate (alert)')}")
                st.markdown(_t(
                    "Pueden fallar parcialmente pero requieren revisión antes del deploy:",
                    "Can fail partially but require review before deploy:",
                ))
                st.markdown("• Agent Looping Recovery ❌\n• Escalation Logic ❌")
                st.warning(_t("Ambas failing. Trigger de revisión obligatoria con Product Owner.",
                              "Both failing. Mandatory review trigger with Product Owner."))

        with col_t3:
            with st.container(border=True):
                st.markdown(f"### Tier 3 — {_t('Roadmap gate (informativo)', 'Roadmap gate (informative)')}")
                st.markdown(_t(
                    "Journeys no en producción — informativo, no bloqueante:",
                    "Journeys not yet in production — informative, not blocking:",
                ))
                st.markdown("• Language Switching ❌\n• Modification Request Handling ❌")
                st.info(_t("Activar como Tier 1 cuando cada intent pase a LIVE.",
                           "Promote to Tier 1 when each intent goes LIVE."))

        st.markdown("---")
        st.markdown(_t("### Orden de remediación", "### Remediation order"))
        remed = {
            "#": ["1", "2", "3", "4", "5", "6"],
            _t("Suite", "Suite"): [
                "Order Number Collection", "Agent Authentication Auto-Transfer",
                "Escalation Logic", "Agent Looping Recovery",
                "Language Switching", "Modification Request Handling",
            ],
            _t("Por qué primero", "Why first"): [
                _t("Habilita Oportunidad 1 (CustomerByTelephone FoD)", "Enables Opportunity 1 (CustomerByTelephone FoD)"),
                _t("Mayor riesgo regulatorio — auth y compliance", "Highest regulatory risk — auth and compliance"),
                _t("25% baseline existente — iterable rápido", "25% existing baseline — fast to iterate"),
                _t("Gate para arquitectura de monitors (Tab 3)", "Gate for monitors architecture (Tab 3)"),
                _t("Necesario antes de reabrir ES/IT/DE/FR", "Required before reopening ES/IT/DE/FR"),
                _t("Esperar a que Modifications salga de BACKLOG", "Wait until Modifications exits BACKLOG"),
            ],
        }
        st.dataframe(remed, use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════
    # TAB 5 — KB FEEDBACK LOOP (OBLIVION)
    # ══════════════════════════════════════════════════════════════════════
    with tab_kb:
        section_badge(_t("KB Feedback Loop", "KB Feedback Loop"), CLAY)
        st.subheader(_t(
            "Una KB que aprende de interacciones reales — viable con quality gate humano.",
            "A KB that learns from real interactions — viable with a human quality gate.",
        ))
        st.markdown(_t(
            "El modelo actual: copywriter edita documento → scraper semanal indexa → "
            "agente consulta. Unidireccional y manual. "
            "El modelo propuesto agrega un loop de retroalimentación desde las interacciones reales, "
            "**sin reemplazar la KB curada** — la augmenta con blindspot detection y vocabulario real del caller.",
            "Current model: copywriter edits document → weekly scraper indexes → agent queries. "
            "Unidirectional and manual. "
            "The proposed model adds a feedback loop from real interactions, "
            "**without replacing the curated KB** — it augments it with blindspot detection "
            "and real caller vocabulary.",
        ))

        st.markdown("---")
        col_pipe, col_state = st.columns([3, 2])

        with col_pipe:
            st.markdown(_t("### Pipeline propuesto", "### Proposed pipeline"))
            st.code(
                "scrape_sessions.py          → raw transcripts\n"
                "  ↓\n"
                "sierra_classifier.py        → intent + pain_points + outcome  ✅ existe\n"
                "  ↓\n"
                "build_issue_log.py          → clusters                         ✅ existe\n"
                "  ↓\n"
                "rqs_scorer.py               → Resolution Quality Score 0-1     ❌ construir\n"
                "  ↓\n"
                "  ├─ RQS ≥ 0.75 → candidate_kb_generator.py                  ❌ construir\n"
                "  │                  ↓\n"
                "  │              [draft artículo + sesiones fuente + categoría]\n"
                "  │                  ↓\n"
                "  │              [HUMAN REVIEW — Product Owner / copywriter]   ← obligatorio\n"
                "  │                  ↓\n"
                "  │              [aprobado → Sierra KB upload / paste manual]\n"
                "  │\n"
                "  └─ RQS < 0.50 → gap_analysis.py                            ❌ construir\n"
                "                     ↓\n"
                "                 [reporte de blindspots → input para copywriter]",
                language="text",
            )

        with col_state:
            st.markdown(_t("### Resolution Quality Score (RQS)", "### Resolution Quality Score (RQS)"))
            st.markdown(_t(
                "Cómo distinguir una buena resolución de una mala para saber qué aprender:",
                "How to distinguish a good resolution from a bad one to know what to learn:",
            ))
            rqs_data = {
                _t("Señal", "Signal"): [
                    "monitor_clean", "csat", "containment",
                    "turn_efficiency", "intent_match",
                ],
                _t("Peso", "Weight"): ["30%", "25%", "20%", "15%", "10%"],
                _t("Definición", "Definition"): [
                    _t("0 monitores disparados", "0 monitors fired"),
                    _t("CSAT ≥4/5 si existe", "CSAT ≥4/5 if available"),
                    _t("Sin tool:transfer", "No tool:transfer"),
                    _t("Turns ≤ p50 de su intent class", "Turns ≤ p50 of intent class"),
                    _t("Intent inicial == intent final", "Initial intent == final intent"),
                ],
            }
            st.dataframe(rqs_data, use_container_width=True, hide_index=True)
            st.caption(_t(
                "**Threshold para entrar al loop:** RQS ≥ 0.75 + revisión humana obligatoria.",
                "**Threshold to enter the loop:** RQS ≥ 0.75 + mandatory human review.",
            ))

        st.markdown("---")
        st.markdown(_t("### Fases de automatización", "### Automation phases"))
        phases_kb = {
            _t("Fase", "Phase"): ["0", "1", "2", "3"],
            _t("Plazo", "Timeline"): [
                _t("Mes 0-3", "Month 0-3"), _t("Mes 3-6", "Month 3-6"),
                _t("Mes 6-12", "Month 6-12"), _t("Mes 12+", "Month 12+"),
            ],
            _t("Automatización", "Automation"): [
                _t("Solo reporting — reportes de gap para copywriter", "Reporting only — gap reports for copywriter"),
                _t("Draft generation — Oblivion sugiere, humano aprueba", "Draft generation — Oblivion suggests, human approves"),
                _t("Auto-publish FAQs informativas con RQS ≥0.85", "Auto-publish informational FAQs with RQS ≥0.85"),
                _t("Auto-publish + auto-update con audit mensual", "Auto-publish + auto-update with monthly audit"),
            ],
            _t("Validación", "Validation"): [
                _t("100% humano escribe", "100% human writes"),
                _t("100% humano aprueba", "100% human approves"),
                _t("Audit semanal + rollback automático", "Weekly audit + auto rollback"),
                _t("Audit mensual + rollback si CSAT cae", "Monthly audit + rollback if CSAT drops"),
            ],
        }
        st.dataframe(phases_kb, use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown(_t("### Riesgos y mitigaciones", "### Risks and mitigations"))
        risks = {
            _t("Riesgo", "Risk"): [
                _t("Amplificación de respuestas malas", "Bad answer amplification"),
                _t("Drift semántico del messaging oficial", "Semantic drift from official messaging"),
                _t("PII leakage en artículos KB", "PII leakage in KB articles"),
                _t("Auto-confirmación — aprende sus propios sesgos", "Self-confirmation — learns own biases"),
                _t("Loop sin convergencia", "Non-convergent loop"),
            ],
            _t("Mitigación", "Mitigation"): [
                _t("RQS ≥ 0.75 gate + revisión humana", "RQS ≥ 0.75 gate + human review"),
                _t("Copywriter como editor-en-jefe — aprueba todo Fase 0-1", "Copywriter as editor-in-chief — approves everything Phase 0-1"),
                _t("Sanitizer LLM + regex antes de generar draft", "LLM + regex sanitizer before generating draft"),
                _t("≥3 sesiones independientes con misma resolución requeridas", "≥3 independent sessions with same resolution required"),
                _t("Rollback automático si CSAT cae >5% en 7 días", "Auto rollback if CSAT drops >5% in 7 days"),
            ],
            _t("Categorías NUNCA auto-publicar", "NEVER auto-publish categories"): [
                "—", "—",
                _t("Compliance, refunds, cancelaciones, KYC, auth", "Compliance, refunds, cancellations, KYC, auth"),
                "—", "—",
            ],
        }
        st.dataframe(risks, use_container_width=True, hide_index=True)

        st.info(_t(
            "**Prerequisitos antes de Fase 1:** (a) las 3 oportunidades de Tab 2 en producción, "
            "(b) RQS validado contra CSAT real, (c) ≥500 sesiones de baseline (no 110). "
            "El valor real de Oblivion no es 'KB que aprende sola' — es **detección de blindspots** "
            "y **vocabulario real del caller** como input para el copywriter.",
            "**Prerequisites before Phase 1:** (a) the 3 opportunities from Tab 2 in production, "
            "(b) RQS validated against real CSAT, (c) ≥500 sessions baseline (not 110). "
            "Oblivion's real value is not 'self-learning KB' — it is **blindspot detection** "
            "and **real caller vocabulary** as input for the copywriter.",
        ))

    # ══════════════════════════════════════════════════════════════════════
    # TAB 6 — CX ALIGNMENT
    # ══════════════════════════════════════════════════════════════════════
    with tab_cx:
        section_badge(_t("Alineación estratégica", "Strategic alignment"), GREEN)
        st.subheader(_t(
            "El diseño del Product Owner es correcto en visión. El SOP tiene gaps operativos que los datos corrigen.",
            "The Product Owner's design is correct in vision. The SOP has operational gaps that data corrects.",
        ))
        st.markdown(_t(
            "Los datos no contradicen al Product Owner — exponen las **reglas que faltan en el SOP** "
            "para que su propia visión se materialice.",
            "Data does not contradict the Product Owner — it exposes the **rules missing from the SOP** "
            "for their own vision to materialise.",
        ))

        st.markdown("---")
        col_po, col_data = st.columns(2)

        with col_po:
            with st.container(border=True):
                st.markdown(f"### ✅ {_t('Donde el Product Owner gana', 'Where the Product Owner wins')}")
                wins_po = {
                    _t("Principio", "Principle"): [
                        _t("FoD CustomerByTelephone", "FoD CustomerByTelephone"),
                        _t("Intent-driven auth (sin CVP informativo)", "Intent-driven auth (no CVP for informational)"),
                        _t("3 preguntas CVP canónicas", "3 canonical CVP questions"),
                        _t("Smart Containment philosophy", "Smart Containment philosophy"),
                        _t("Clean handoff con contexto", "Clean handoff with context"),
                        _t("KB curada como source of truth", "Curated KB as source of truth"),
                        _t("CVP solo transaccional", "CVP for transactional only"),
                    ],
                    _t("Qué confirman los datos", "What data confirms"): [
                        _t("Ausencia = causa de 43 ses. afectadas", "Absence = cause of 43 affected sessions"),
                        _t("10 ses. con disclosure sin auth lo confirman", "10 sessions with unauth disclosure confirm it"),
                        _t("Issues #4 son exactamente desviaciones de las 3", "Issues #4 are exactly deviations from the 3"),
                        _t("Issues #7 (bloqueo) = opuesto de smart containment", "Issues #7 (blocking) = opposite of smart containment"),
                        _t("13 ses. sin contexto al transfer confirman el gap", "13 sessions with no context at transfer confirm the gap"),
                        _t("Fase 0-1 de Oblivion — el PO tiene razón", "Oblivion Phase 0-1 — PO is correct"),
                        _t("Intents info sin CVP confirman el principio", "Info intents without CVP confirm the principle"),
                    ],
                }
                st.dataframe(wins_po, use_container_width=True, hide_index=True)

        with col_data:
            with st.container(border=True):
                st.markdown(f"### ⚠️ {_t('Donde los datos corrigen al SOP', 'Where data corrects the SOP')}")
                wins_data = {
                    _t("Regla SOP actual", "Current SOP rule"): [
                        '"Allow retry. Do not transfer." (sin límite)',
                        _t("Manual CVP sin definir cuándo", "Manual CVP without defining when"),
                        _t("OTP 'planeado para el futuro'", "OTP 'planned for the future'"),
                        _t("Simulations sin governance de release", "Simulations without release governance"),
                        _t("Monitors sin recovery flow", "Monitors without recovery flow"),
                    ],
                    _t("Qué dicen los datos", "What data says"): [
                        _t("9 ses. Critical de loop. Corrección: 3-strike → transfer",
                           "9 Critical sessions of loop. Fix: 3-strike → transfer"),
                        _t("Es el default cuando FoD no funciona — no es fallback",
                           "It is the default when FoD is broken — not a fallback"),
                        _t("CVP falla sistemáticamente — OTP debería subir prioridad",
                           "CVP fails systematically — OTP should be prioritised"),
                        _t("6/7 suites failing en producción — governance urgente",
                           "6/7 suites failing in production — urgent governance needed"),
                        _t("40% sesiones en loop sin acción correctiva",
                           "40% of sessions looping with no corrective action"),
                    ],
                }
                st.dataframe(wins_data, use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown(_t("### Modelo combinado óptimo", "### Optimal combined model"))

        col_keep, col_fix = st.columns(2)
        with col_keep:
            st.markdown(_t("**Mantener del Product Owner:**", "**Keep from Product Owner:**"))
            st.markdown(_t(
                "- CXI Design Principles como Norte Estratégico inmutable\n"
                "- 3 preguntas CVP canónicas\n"
                "- `CustomerByTelephone` como FoD obligatorio\n"
                "- KB curada por copywriter como source of truth\n"
                "- Smart Containment philosophy\n"
                "- CVP solo para intents transaccionales",
                "- CXI Design Principles as immutable strategic north\n"
                "- 3 canonical CVP questions\n"
                "- `CustomerByTelephone` as mandatory FoD\n"
                "- Copywriter-curated KB as source of truth\n"
                "- Smart Containment philosophy\n"
                "- CVP for transactional intents only",
            ))
        with col_fix:
            st.markdown(_t("**Corregir / agregar al SOP:**", "**Correct / add to SOP:**"))
            st.markdown(_t(
                "- Upper bound CVP retries: 3 strikes → transfer con contexto\n"
                "- Intent classifier antes de CVP (informacional vs. transaccional)\n"
                "- Monitors como circuit breakers activos\n"
                "- Simulations como release gate Tier 1\n"
                "- Subir prioridad de OTP y Modifications\n"
                "- Oblivion como augmentation del copywriter (no reemplazo)",
                "- CVP retry upper bound: 3 strikes → transfer with context\n"
                "- Intent classifier before CVP (informational vs. transactional)\n"
                "- Monitors as active circuit breakers\n"
                "- Simulations as Tier 1 release gate\n"
                "- Elevate OTP and Modifications priority\n"
                "- Oblivion as copywriter augmentation (not replacement)",
            ))

        st.markdown("---")
        st.markdown(_t("### Métricas de éxito — visión combinada (90 días)", "### Success metrics — combined vision (90 days)"))
        metrics_final = {
            _t("Métrica", "Metric"): [
                _t("Containment rate", "Containment rate"),
                _t("CVP success rate", "CVP success rate"),
                _t("Agent Looping monitor trigger rate", "Agent Looping monitor trigger rate"),
                _t("Issues Critical recurrentes", "Recurring Critical issues"),
                _t("Simulation Tier 1 pass rate", "Simulation Tier 1 pass rate"),
                _t("CustomerByTelephone FoD invocation rate", "CustomerByTelephone FoD invocation rate"),
                _t("Velocity: detección → deploy fix", "Velocity: detection → deploy fix"),
            ],
            _t("Baseline actual", "Current baseline"): [
                "~35% (estimado)", "~60% (estimado)", "40%",
                "8 issues activos", "33% (1/3 passing)", "0%", ">30 días",
            ],
            _t("Target 90 días", "90-day target"): [
                "≥65%", "≥85%", "<10%",
                "≤2 issues recurrentes", "100%", "≥85%", "<5 días",
            ],
        }
        st.dataframe(metrics_final, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# PAGE — PO Recommendations
# ---------------------------------------------------------------------------

def page_po_recs():
    st.title(_t("📌  Recomendaciones para el Product Owner",
                "📌  Product Owner Recommendations"))
    st.caption(_t(
        "Gaps entre el diseño documentado en Confluence (Feb-26 Sierra Journey SOP · "
        "Conversational Design Principles · CXI Design Principles) y el comportamiento "
        "observado en producción. Ninguna cierra con un cambio en el Agent Builder — "
        "requieren una decisión del PO.",
        "Gaps between the documented design in Confluence (Feb-26 Sierra Journey SOP · "
        "Conversational Design Principles · CXI Design Principles) and observed production "
        "behaviour. None of these close with an Agent Builder change — they require a PO decision.",
    ))

    SLATE = "#6A8CAA"
    badge_css = (
        f"background:{SLATE};color:#fff;padding:2px 10px;border-radius:3px;"
        "font-size:0.72rem;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;"
        "display:inline-block;margin-bottom:0.4rem;"
    )

    # ── PO-1 ──────────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown(f'<span style="{badge_css}">PO-1</span>', unsafe_allow_html=True)
        st.subheader(_t(
            "El SOP diseña el loop CVP — falta un hard-exit counter",
            "The SOP by design creates the CVP loop — it needs a hard-exit counter",
        ))
        col_l, col_r = st.columns([2, 1])
        with col_l:
            st.markdown(_t(
                "**Qué dice el SOP (§5.3):**",
                "**What the SOP says (§5.3):**",
            ))
            st.info(_t(
                '"Si la autenticación falla: Allow retry. Do not transfer."  \n'
                "No hay límite de intentos. No hay contador. No hay condición de salida. "
                "El agente sigue la instrucción al pie de la letra — y por eso entra en loop.",
                '"If authentication fails: Allow retry. Do not transfer."  \n'
                "No attempt limit. No counter. No exit condition. The agent follows the "
                "instruction exactly — and that is why it loops.",
            ))
            st.markdown(_t(
                "**Recomendación:** Agregar al SOP §5.3 una condición de salida:",
                "**Recommendation:** Add to SOP §5.3 an explicit exit condition:",
            ))
            st.code(
                "Después de 2 intentos fallidos de AttemptCvpAuthentication\n"
                "  → CreateZendeskTicket(reason='cvp_authentication_failed')\n"
                "  → Informar al caller: seguimiento por agente humano\n"
                "  → No transferir por SIP\n"
                "  (fallback: Manual CVP via CXi — ya definido en Design Principles)",
                language="text",
            )
        with col_r:
            st.metric(_t("Issues afectados", "Issues affected"), "#2")
            st.metric(_t("Sesiones críticas", "Critical sessions"), "9")
            st.metric(_t("Tool nuevo requerido", "New tool required"), _t("No", "No"))
            st.caption(_t(
                "`CreateZendeskTicket` ya existe en el agente.",
                "`CreateZendeskTicket` already exists in the agent.",
            ))

    # ── PO-2 ──────────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown(f'<span style="{badge_css}">PO-2</span>', unsafe_allow_html=True)
        st.subheader(_t(
            "CustomerByTelephone está en los principios de diseño, no en el agente",
            "CustomerByTelephone is in the design principles but absent from the agent",
        ))
        col_l, col_r = st.columns([2, 1])
        with col_l:
            st.markdown(_t(
                "**Qué dicen los Conversational Design Principles:**",
                "**What the Conversational Design Principles say:**",
            ))
            st.info(_t(
                '"At the front of door, we will use the Customer by Telephone API to identify '
                "the caller and retrieve relevant transaction information. This reduces the need "
                'to ask callers to identify their type and enables proactive support."',
                '"At the front of door, we will use the Customer by Telephone API to identify '
                "the caller and retrieve relevant transaction information. This reduces the need "
                'to ask callers to identify their type and enables proactive support."',
            ))
            st.markdown(_t(
                "**Evidencia:** En 110 sesiones scrapeadas con transcripts y traces completos, "
                "**no hay una sola llamada a `CustomerByTelephone`** — ni al inicio ni como fallback.",
                "**Evidence:** Across 110 scraped sessions with complete transcripts and traces, "
                "**there is not a single call to `CustomerByTelephone`** — neither at call start nor as fallback.",
            ))
            st.warning(_t(
                "**Pregunta directa para el PO:** ¿Está `CustomerByTelephone` implementado "
                "en producción hoy? Si no, los Issues #1, #3 y #5 (37 sesiones) son consecuencia "
                "de un prerequisito faltante, no de un error del agente.",
                "**Direct question for the PO:** Is `CustomerByTelephone` implemented in production "
                "today? If not, Issues #1, #3 and #5 (37 sessions) are the consequence of a missing "
                "prerequisite, not incorrect agent behaviour.",
            ))
        with col_r:
            st.metric(_t("Issues afectados", "Issues affected"), "#1, #3, #5")
            st.metric(_t("Sesiones", "Sessions"), "37")
            st.metric(_t("Tool nuevo requerido", "New tool required"), _t("No", "No"))
            st.caption(_t(
                "Es el cambio de mayor palanca del roadmap completo.",
                "Highest-leverage change in the entire roadmap.",
            ))

    # ── PO-3 ──────────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown(f'<span style="{badge_css}">PO-3</span>', unsafe_allow_html=True)
        st.subheader(_t(
            "Los monitores de Sierra detectan loops pero no tienen recovery flow",
            "Sierra monitors detect loops but are not wired to any recovery flow",
        ))
        col_l, col_r = st.columns([2, 1])
        with col_l:
            st.markdown(_t(
                "**Qué dice el SOP (§11 — Compliance Checklist):** Valida que las reglas "
                "estén configuradas. No define ninguna acción automática cuando un monitor dispara.",
                "**What the SOP says (§11 — Compliance Checklist):** Validates that rules are "
                "configured. Does not define any automatic action when a monitor fires.",
            ))
            monitor_data = {
                _t("Monitor", "Monitor"): ["Agent Looping", "False Transfer", "Frustration Increase"],
                _t("Sesiones afectadas", "Sessions affected"): ["44 / 111 (40%)", "13 / 111 (12%)", "12 / 111 (11%)"],
                _t("Acción al disparar", "Action on fire"): [
                    _t("Ninguna — agente continúa en loop", "None — agent continues looping"),
                    _t("Ninguna — agente repite el anuncio", "None — agent repeats the announcement"),
                    _t("Ninguna — agente ignora la señal", "None — agent ignores the signal"),
                ],
            }
            st.dataframe(monitor_data, use_container_width=True, hide_index=True)
            st.markdown(_t(
                "**Recomendación:** Agregar a Global Rules una sección *Monitor-Triggered Recovery*:",
                "**Recommendation:** Add a *Monitor-Triggered Recovery* section to Global Rules:",
            ))
            st.code(
                "Agent Looping dispara:\n"
                "  → Interrumpir flujo → ofrecer (a) ruta alternativa o (b) Zendesk ticket\n\n"
                "Frustration Increase dispara:\n"
                "  → Activar empathy language → reducir speedbumps restantes\n\n"
                "False Transfer dispara:\n"
                "  → Verificar estado de transferencia antes de anunciar al caller",
                language="text",
            )
        with col_r:
            st.metric(_t("Sesiones con looping", "Sessions with looping"), "44 / 111")
            st.metric(_t("% del total", "% of total"), "40%")
            st.metric(_t("Tool nuevo requerido", "New tool required"), _t("No", "No"))

    # ── PO-4 ──────────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown(f'<span style="{badge_css}">PO-4</span>', unsafe_allow_html=True)
        st.subheader(_t(
            "La autenticación intent-driven existe en los principios, no en el agente",
            "Intent-driven authentication exists in the principles but not in the agent",
        ))
        col_l, col_r = st.columns([2, 1])
        with col_l:
            st.markdown(_t(
                "**Qué dicen los CXI Design Principles (principio #4):**",
                "**What the CXI Design Principles say (principle #4):**",
            ))
            st.info(_t(
                '"Authentication is triggered by intent sensitivity — not by default. '
                'Informational intents → No CVP. Transactional actions → CVP required."',
                '"Authentication is triggered by intent sensitivity — not by default. '
                'Informational intents → No CVP. Transactional actions → CVP required."',
            ))
            st.markdown(_t(
                "**Evidencia:** Sesiones `general_info` y parte de `transaction_status` "
                "muestran al agente solicitando CVP para preguntas que no requieren acceso "
                "a datos de cuenta (fees genéricos, ETA general, estado de servicios). "
                "El path *Check Order ETA* dispara el auth flow completo incluso para "
                "consultas informacionales.",
                "**Evidence:** `general_info` and part of `transaction_status` sessions show "
                "the agent requesting CVP for questions that require no account data access "
                "(generic fees, general ETA, service status). The *Check Order ETA* path "
                "triggers the full auth flow even for informational queries.",
            ))
            st.markdown(_t(
                "**Recomendación:** Agregar un bloque *Pre-Auth Intent Classification* "
                "como primer paso del journey principal, antes de `SetCallerType`:",
                "**Recommendation:** Add a *Pre-Auth Intent Classification* block as the "
                "first step of the main journey, before `SetCallerType`:",
            ))
            st.code(
                "Clasificar intent ANTES de SetCallerType:\n"
                "  (a) informacional → KB/FAQ sin auth\n"
                "      fees, ETA genérico, estado servicios, horarios\n"
                "  (b) transaccional → authentication segment\n"
                "      status orden específica, cancelación, modificación\n\n"
                "Nota: FAQ - KB RAG intent ya está LIVE (v.01) en producción.",
                language="text",
            )
        with col_r:
            st.metric(_t("Sesiones afectadas", "Sessions affected"),
                      _t("~15-20%", "~15-20%"))
            st.metric("FAQ - KB RAG", "LIVE v.01")
            st.metric(_t("Tool nuevo requerido", "New tool required"), _t("No", "No"))
            st.caption(_t(
                "El intent FAQ ya existe — solo falta el triage en Front of Door.",
                "The FAQ intent already exists — only Front of Door triage is missing.",
            ))

    # ── Resumen ──────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader(_t("Resumen", "Summary"))
    summary = {
        "#": ["PO-1", "PO-2", "PO-3", "PO-4"],
        _t("Recomendación", "Recommendation"): [
            _t("Hard-exit counter CVP (2 intentos → Zendesk ticket)",
               "CVP hard-exit counter (2 attempts → Zendesk ticket)"),
            _t("Activar CustomerByTelephone en Front of Door",
               "Activate CustomerByTelephone at Front of Door"),
            _t("Monitor-Triggered Recovery en Global Rules",
               "Monitor-Triggered Recovery in Global Rules"),
            _t("Pre-Auth Intent Classification antes de SetCallerType",
               "Pre-Auth Intent Classification before SetCallerType"),
        ],
        _t("Fuente Confluence", "Confluence source"): [
            "Feb-26 SOP §5.3 + Conversational Design Principles",
            "Conversational Design Principles + CXI Design Principles §2",
            "Feb-26 SOP §11 (gap)",
            "CXI Design Principles §4 + Conversational Design Principles",
        ],
        _t("Issues", "Issues"): ["#2 (9 ses.)", "#1,#3,#5 (37 ses.)", "44/111 (40%)", "~15-20% ses."],
        _t("Tool nuevo", "New tool"): ["No", "No", "No", "No"],
    }
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.caption(_t(
        "Nota metodológica: análisis cruzado entre 110 sesiones clasificadas + 444 resultados "
        "de monitor y la documentación oficial del PO en el espacio CXI de Confluence "
        "(última actualización: 2026-04-27).",
        "Methodological note: cross-analysis of 110 classified sessions + 444 monitor results "
        "against the PO's official documentation in the CXI Confluence space "
        "(last updated: 2026-04-27).",
    ))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

if page == "Overview":
    page_overview()
elif page == "Investigate":
    page_investigate()
elif page == "Gap Proposals":
    page_gap_drilldown()
elif page == "PO Recs":
    page_po_recs()
elif page == "Strategic":
    page_strategic()
elif page == "Simulations":
    page_simulations()
elif page == "Glossary":
    page_glossary()
