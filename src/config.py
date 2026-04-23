"""Configuration — loads .env and exposes typed constants.

Dashboard-only mode: when running the Streamlit dashboard (read-only against
the scraped DB), the Sierra credentials are NOT required — the helpers below
return empty strings for missing vars and the sierra_client will raise a
clear error only if a scrape is actually attempted.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _opt(name: str, default: str = "") -> str:
    """Return the env var if set, else `default`. Never raises."""
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else default


def require_sierra_credentials() -> None:
    """Call this from the scrapers (not the dashboard) to enforce credentials."""
    missing = [n for n in ("SIERRA_BASE_URL", "SIERRA_AGENT_ID",
                           "SIERRA_WORKSPACE_VERSION_ID", "SIERRA_COOKIE",
                           "SIERRA_CSRF_TOKEN")
               if not os.getenv(n, "").strip()]
    if missing:
        raise EnvironmentError(
            "Missing Sierra credentials: " + ", ".join(missing)
            + ". Fill them in .env (local) or in st.secrets (Streamlit Cloud)."
        )


SIERRA_BASE_URL     = _opt("SIERRA_BASE_URL", "https://euronet.sierra.ai")
SIERRA_GRAPHQL_PATH = _opt("SIERRA_GRAPHQL_PATH", "/-/api/graphql")
SIERRA_GRAPHQL_URL  = SIERRA_BASE_URL.rstrip("/") + SIERRA_GRAPHQL_PATH

SIERRA_AGENT_ID             = _opt("SIERRA_AGENT_ID", "bot-01K5WBQ9Q5VK3FGTV2VVDT5T4C")
SIERRA_WORKSPACE_VERSION_ID = _opt("SIERRA_WORKSPACE_VERSION_ID", "workspaceversion-01KPV0JND8QVSW9H7ZQMY4PR90")
SIERRA_COOKIE               = _opt("SIERRA_COOKIE")
SIERRA_CSRF_TOKEN           = _opt("SIERRA_CSRF_TOKEN")
SIERRA_USER_AGENT           = os.getenv(
    "SIERRA_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
).strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH  = DATA_DIR / "sierra.db"
DOCS_DIR = DATA_DIR / "sierra_docs"

# Request pacing (seconds between calls). Sierra has no documented rate limit;
# play nice by default.
REQUEST_DELAY_SECONDS = float(os.getenv("SIERRA_REQUEST_DELAY", "0.3"))

# SSL verification (set "false" behind a corporate proxy that re-signs TLS).
SSL_VERIFY: bool | str = os.getenv("SSL_VERIFY", "true").lower() != "false"

# Optional HTTP(S) proxy.
HTTP_PROXY  = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
PROXIES: dict[str, str] | None = (
    {"http": HTTP_PROXY, "https": HTTPS_PROXY}
    if HTTP_PROXY or HTTPS_PROXY
    else None
)
