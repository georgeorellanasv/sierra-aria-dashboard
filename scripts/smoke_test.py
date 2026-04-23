"""
Connectivity smoke test: makes ONE call each to the 4 critical endpoints,
prints a short summary. If any fails, the cookie/CSRF has expired and .env
needs refreshing before running the real scraper.

Run from project root:
  python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, queries
from src.sierra_client import SierraAuthError, SierraClient, SierraGraphQLError


def main() -> int:
    print(f"GraphQL URL : {config.SIERRA_GRAPHQL_URL}")
    print(f"Agent       : {config.SIERRA_AGENT_ID}")
    print(f"Workspace v : {config.SIERRA_WORKSPACE_VERSION_ID}")
    print()

    client = SierraClient()

    tests = [
        ("conversation list (no filters)", _test_conversation_list, client),
        ("journeys list",                  _test_journeys_list,     client),
        ("knowledge sources",              _test_knowledge_sources, client),
    ]
    results: list[tuple[str, bool, str]] = []
    for name, fn, arg in tests:
        try:
            summary = fn(arg)
            results.append((name, True, summary))
            print(f"  OK    {name:40}  {summary}")
        except SierraAuthError as e:
            results.append((name, False, f"AUTH: {e}"))
            print(f"  AUTH  {name:40}  {e}")
        except SierraGraphQLError as e:
            results.append((name, False, f"GQL: {e}"))
            print(f"  GQL   {name:40}  {e}")
        except Exception as e:
            results.append((name, False, f"ERR: {e!r}"))
            print(f"  FAIL  {name:40}  {e!r}")

    print()
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"{n_ok}/{len(results)} OK")
    return 0 if n_ok == len(results) else 1


def _test_conversation_list(client: SierraClient) -> str:
    data = client.query(
        queries.CONVERSATION_LIST,
        {
            "agentId":              client.agent_id,
            "search":               None,
            "timeRange":            None,
            "tags":                 [],
            "journeys":             [],
            "reviewStatuses":       [],
            "releases":             [],
            "targets":              [],
            "channels":             [],
            "containsUserMessages": False,
            "filterQuery":          None,
        },
        operation_label="conversationListQuery",
    )
    node = data.get("node") or {}
    logs = (node.get("logs") or {}).get("edges") or []
    sample_ids = [e.get("node", {}).get("id") for e in logs[:3]]
    return f"got {len(logs)} logs, sample ids: {sample_ids}"


def _test_journeys_list(client: SierraClient) -> str:
    data = client.query_workspace(
        queries.JOURNEYS_LIST,
        {"enabled": True},
        operation_label="useJourneysWorkspaceVersionQuery",
    )
    wv = data.get("workspaceVersionByID") or {}
    # Response has nested fragments; try to locate journeys list generically.
    def find_journey_names(obj, found):
        if isinstance(obj, dict):
            if "journeys" in obj and isinstance(obj["journeys"], (list, dict)):
                j = obj["journeys"]
                edges = j.get("edges") if isinstance(j, dict) else None
                items = edges or (j if isinstance(j, list) else [])
                for it in items:
                    node = it.get("node") if isinstance(it, dict) else None
                    name = (node or it).get("name") if isinstance((node or it), dict) else None
                    if name:
                        found.append(name)
            for v in obj.values():
                find_journey_names(v, found)
        elif isinstance(obj, list):
            for v in obj:
                find_journey_names(v, found)
    found: list[str] = []
    find_journey_names(wv, found)
    return f"found {len(found)} journeys, names: {found[:8]}"


def _test_knowledge_sources(client: SierraClient) -> str:
    data = client.query(
        queries.KNOWLEDGE_SOURCES,
        {
            "botId":                       client.agent_id,
            "workspaceId":                 "",
            "workspaceVersionId":          client.workspace_version_id,
            "useWorkspaceKnowledgeSources": False,
        },
        operation_label="knowledgeSourcesQuery",
    )
    sources = data.get("allKnowledgeSources") or []
    names = [(s.get("name"), s.get("id")) for s in sources]
    return f"got {len(sources)} sources: {names}"


if __name__ == "__main__":
    raise SystemExit(main())
