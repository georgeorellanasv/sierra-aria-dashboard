"""
Scrape the Sierra agent build artifacts into data/sierra.db:

  - Every journey (only 1 in the Aria workspace, but we walk programmatically)
  - Every named block inside each journey (these are the sidebar entries:
    Intents Where User Needs..., Select Order, Check Order Status, ...)
  - Every tool attached to the agent (journeyTools)
  - Every KB source and every article in each source
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, queries, sierra_db
from src.lexical_md import find_all_named_blocks, full_markdown
from src.sierra_client import SierraClient

logger = logging.getLogger("scrape_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)


# ---------------------------------------------------------------------------
# Journeys
# ---------------------------------------------------------------------------

def fetch_journey_ids(client: SierraClient) -> list[str]:
    data = client.query_workspace(
        queries.JOURNEYS_LIST,
        {"enabled": True},
        operation_label="useJourneysWorkspaceVersionQuery",
    )
    wv = data.get("workspaceVersionByID") or {}
    edges = ((wv.get("journeys") or {}).get("edges")) or []
    return [(e.get("node") or {}).get("id") for e in edges if e.get("node")]


def fetch_journey_detail(client: SierraClient, journey_id: str) -> dict:
    v = {
        "workspaceVersionId":   client.workspace_version_id,
        "journeyId":            journey_id,
        "shouldFetchSnapshot":  False,
    }
    data = client.query(queries.JOURNEY_DETAIL, v,
                        operation_label="useJourneyWorkspaceVersionQuery")
    return ((data.get("workspaceVersionByID") or {}).get("journeyByID")) or {}


def store_journey(conn, journey_id: str, detail: dict) -> None:
    # Prefer editing version (latest Staging); fall back to published.
    version = detail.get("editingJourneyVersion") or detail.get("publishedJourneyVersion") or {}
    version_type = "editing" if detail.get("editingJourneyVersion") else "published"

    editor_state_text = version.get("editorStateJson") or "{}"
    try:
        editor_state = json.loads(editor_state_text)
    except json.JSONDecodeError:
        editor_state = {}

    markdown = full_markdown(editor_state) if editor_state else ""

    sierra_db.upsert(conn, "journeys", {
        "id":                journey_id,
        "name":              detail.get("name"),
        "version_type":      version_type,
        "version_id":        version.get("id"),
        "description":       version.get("description") or "",
        "criteria":          version.get("criteria") or "",
        "editor_state_json": editor_state_text,
        "markdown":          markdown,
        "tools_json":        sierra_db.dumps(version.get("tools")),
    })

    conn.execute("DELETE FROM journey_blocks WHERE journey_id=?", (journey_id,))
    for i, blk in enumerate(find_all_named_blocks(editor_state)):
        conn.execute(
            "INSERT INTO journey_blocks(journey_id, idx, block_uuid, block_type, "
            "block_name, markdown) VALUES (?, ?, ?, ?, ?, ?)",
            (journey_id, i, blk["uuid"], blk["type"], blk["name"], blk["markdown"]),
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def fetch_tools(client: SierraClient) -> list[dict]:
    data = client.query_workspace(
        queries.TOOLS_LIST,
        {},
        operation_label="useToolsAsyncWorkspaceVersionQuery",
    )
    wv = data.get("workspaceVersionByID") or {}
    edges = ((wv.get("journeyTools") or {}).get("edges")) or []
    return [e.get("node") for e in edges if e.get("node")]


def store_tool(conn, tool: dict) -> None:
    sierra_db.upsert(conn, "tools", {
        "id":          tool.get("id"),
        "name":        tool.get("name"),
        "description": tool.get("description"),
        "type":        tool.get("type"),
        "params_json": sierra_db.dumps(tool.get("paramsJson") or tool),
    })


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

def fetch_kb_sources(client: SierraClient) -> list[dict]:
    data = client.query(
        queries.KNOWLEDGE_SOURCES,
        {
            "botId":                        client.agent_id,
            "workspaceId":                  "",
            "workspaceVersionId":           client.workspace_version_id,
            "useWorkspaceKnowledgeSources": False,
        },
        operation_label="knowledgeSourcesQuery",
    )
    return data.get("allKnowledgeSources") or []


# The captured articles query hardcodes `first: 50`. Bump it so one call returns
# up to 1000 articles — enough for Aria's ~197 Public FAQs.
_KB_ARTICLES_QUERY_FULL = re.sub(
    r"articles\(first:\s*\d+",
    "articles(first: 1000",
    queries.KB_ARTICLES_LIST,
)


def fetch_articles_in_source(client: SierraClient, source_id: str) -> list[dict]:
    v = {
        "sourceId":                      source_id,
        "workspaceId":                   "",
        "workspaceVersionId":            client.workspace_version_id,
        "useWorkspaceKnowledgeArticles": False,
        "orderBy":                       "UPDATED_TIME_DESC",
    }
    data = client.query(_KB_ARTICLES_QUERY_FULL, v,
                        operation_label="useKnowledgeSourceArticlesQuery")
    ks = data.get("knowledgeSourceByID") or {}
    edges = ((ks.get("articles") or {}).get("edges")) or []
    return [e.get("node") for e in edges if e.get("node")]


def fetch_article_detail(client: SierraClient, source_id: str, url_hash: str) -> dict:
    v = {
        "sourceId":                     source_id,
        "urlHash":                      url_hash,
        "workspaceId":                  "",
        "workspaceVersionId":           client.workspace_version_id,
        "useWorkspaceKnowledgeArticle": False,
    }
    data = client.query(queries.KB_ARTICLE_DETAIL, v,
                        operation_label="articleDetailPanelQuery")
    ks = data.get("knowledgeSourceByID") or {}
    return ks.get("articleByUrlHash") or ks.get("article") or {}


def store_kb_source(conn, src: dict) -> None:
    sierra_db.upsert(conn, "kb_sources", {
        "id":            src.get("id"),
        "name":          src.get("name"),
        "type":          src.get("type"),
        "article_count": src.get("articleCount"),
        "origin_json":   sierra_db.dumps(src.get("originConfig") or src),
    })


def _scalar(v):
    if isinstance(v, (dict, list)):
        return sierra_db.dumps(v)
    return v


def store_kb_article(conn, source_id: str, art: dict, content: str | None = None) -> None:
    src_url = art.get("sourceUrl") or (art.get("source") or {}).get("url")
    # The detail endpoint returns content as a dict: {body, alternativeTitles}.
    if content is None:
        content_field = art.get("content")
        if isinstance(content_field, dict):
            content = content_field.get("body")
        elif isinstance(content_field, str):
            content = content_field
        else:
            content = art.get("body")
    # `updatedTime` comes as epoch millis; stringify.
    last_updated = art.get("updatedTime") or art.get("lastUpdatedTime")
    if isinstance(last_updated, (int, float)):
        last_updated = str(int(last_updated))
    sierra_db.upsert(conn, "kb_articles", {
        "id":           art.get("urlHash") or art.get("id"),
        "source_id":    source_id,
        "title":        _scalar(art.get("title")),
        "source_url":   _scalar(src_url),
        "content":      _scalar(content),
        "last_updated": _scalar(last_updated),
        "raw_json":     sierra_db.dumps(art),
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-kb-content", action="store_true",
                    help="List KB articles but don't fetch their content (fast)")
    ap.add_argument("--kb-max", type=int, default=None,
                    help="Cap articles fetched per source (for testing)")
    args = ap.parse_args()

    sierra_db.init_db()
    client = SierraClient()

    # 1. Journeys
    journey_ids = fetch_journey_ids(client)
    logger.info("Journeys: %d  %s", len(journey_ids), journey_ids)
    with sierra_db.connect() as conn:
        for jid in journey_ids:
            detail = fetch_journey_detail(client, jid)
            store_journey(conn, jid, detail)
            nblocks = conn.execute(
                "SELECT COUNT(*) FROM journey_blocks WHERE journey_id=?", (jid,)
            ).fetchone()[0]
            logger.info("  %s  name=%r  blocks=%d", jid, detail.get("name"), nblocks)

    # 2. Tools
    tools = fetch_tools(client)
    logger.info("Tools: %d", len(tools))
    with sierra_db.connect() as conn:
        for t in tools:
            store_tool(conn, t)
            logger.info("  tool: %s  %s", t.get("name"), t.get("description") or "")

    # 3. KB sources and articles
    sources = fetch_kb_sources(client)
    logger.info("KB sources: %d", len(sources))
    for src in sources:
        with sierra_db.connect() as conn:
            store_kb_source(conn, src)
        articles = fetch_articles_in_source(client, src["id"])
        logger.info("  source %r  (%s)  articles=%d",
                    src.get("name"), src["id"], len(articles))

        if args.kb_max:
            articles = articles[: args.kb_max]

        for i, art in enumerate(articles, 1):
            url_hash = art.get("urlHash") or art.get("id")
            content = None
            if not args.skip_kb_content and url_hash:
                try:
                    detail = fetch_article_detail(client, src["id"], url_hash)
                    content = detail.get("content") or detail.get("body")
                    # Merge any enrichment fields into art
                    art = {**art, **{k: v for k, v in detail.items() if v is not None}}
                except Exception as e:
                    logger.warning("    article %s failed: %r", url_hash, e)
            with sierra_db.connect() as conn:
                store_kb_article(conn, src["id"], art, content)
            if i % 20 == 0 or i == len(articles):
                logger.info("    [%d/%d] articles stored", i, len(articles))

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
