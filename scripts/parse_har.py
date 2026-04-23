"""
Parse the Sierra HAR capture and extract every unique GraphQL operation.

Sierra sends anonymous GraphQL queries, so we identify each by:
  1. Root field name parsed from the query text (e.g. `conversation`, `sessions`).
  2. Hash of the query body so different queries hitting the same root don't collide.

Outputs:
  - Summary to stdout (root field, distinct query-hash count, total requests)
  - data/graphql_ops/<root>__<hash>.json — one file per distinct query with:
      query text, sample variables, response status, sample response JSON
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HAR_PATH = Path(__file__).resolve().parent.parent / "data" / "sierra_capture.har"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "graphql_ops"

ROOT_FIELD_RE = re.compile(
    r"(?:query|mutation|subscription)[^{]*\{\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.DOTALL,
)
ANON_ROOT_RE = re.compile(r"^\s*\{\s*([A-Za-z_][A-Za-z0-9_]*)", re.DOTALL)


def infer_root_field(query: str) -> str:
    if not query:
        return "unknown"
    m = ROOT_FIELD_RE.search(query)
    if m:
        return m.group(1)
    m = ANON_ROOT_RE.search(query)
    if m:
        return m.group(1)
    return "unknown"


def query_hash(query: str) -> str:
    normalized = re.sub(r"\s+", " ", (query or "").strip())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def load_har(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_graphql_entries(har: dict) -> list[dict]:
    entries = har.get("log", {}).get("entries", [])
    gql = []
    for e in entries:
        req = e.get("request", {})
        url = req.get("url", "")
        if "/graphql" not in url:
            continue
        if req.get("method") != "POST":
            continue
        post_data = req.get("postData", {}) or {}
        body_text = post_data.get("text")
        if not body_text:
            continue
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            continue
        resp = e.get("response", {}) or {}
        content = resp.get("content", {}) or {}
        resp_text = content.get("text")
        resp_json = None
        if resp_text:
            try:
                resp_json = json.loads(resp_text)
            except json.JSONDecodeError:
                pass
        query_text = body.get("query") or ""
        gql.append({
            "url": url,
            "operationName": body.get("operationName"),
            "query": query_text,
            "root": infer_root_field(query_text),
            "qhash": query_hash(query_text),
            "variables": body.get("variables"),
            "status": resp.get("status"),
            "response": resp_json,
            "started": e.get("startedDateTime"),
        })
    return gql


def main() -> int:
    if not HAR_PATH.exists():
        print(f"HAR not found at {HAR_PATH}", file=sys.stderr)
        return 1
    print(f"Loading {HAR_PATH} ({HAR_PATH.stat().st_size / 1024 / 1024:.1f} MB)...")
    har = load_har(HAR_PATH)
    entries = extract_graphql_entries(har)
    print(f"Found {len(entries)} GraphQL POST entries.\n")

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in entries:
        by_key[(e["root"], e["qhash"])].append(e)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{'Root field':<35} {'Hash':<10} {'Count':>6} {'RespKB':>8}")
    print("-" * 65)

    grouped_by_root: dict[str, int] = defaultdict(int)
    for (root, qhash), samples in sorted(by_key.items()):
        grouped_by_root[root] += len(samples)
        rep = next((s for s in samples if s.get("response")), samples[0])
        resp_size = len(json.dumps(rep.get("response") or {}))
        print(f"{root:<35} {qhash:<10} {len(samples):>6} {resp_size / 1024:>7.1f}")
        out = OUT_DIR / f"{root}__{qhash}.json"
        with out.open("w", encoding="utf-8") as f:
            json.dump({
                "root": root,
                "qhash": qhash,
                "count": len(samples),
                "url": rep["url"],
                "query": rep["query"],
                "sample_variables": rep["variables"],
                "sample_status": rep["status"],
                "sample_response": rep["response"],
                "sample_started": rep["started"],
            }, f, indent=2, ensure_ascii=False)

    print("\nTotals by root field:")
    for root, n in sorted(grouped_by_root.items(), key=lambda x: -x[1]):
        print(f"  {root:<35} {n}")
    print(f"\nWrote {len(by_key)} distinct queries to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
