# Sierra / Aria Analytics

Scrapes the Sierra voice-agent dashboard (`euronet.sierra.ai`) for the Aria
(Ria Money Transfer) agent, stores everything in SQLite, and classifies each
conversation with Sonnet 4.6 to surface concrete improvements for the
`Check Order Status` / `Check Order ETA` journey blocks and their tools.

## What it does

1. **Session scraper** — lists conversations and, for each one, pulls
   transcript, tags, journey IDs, LLM + tool-call traces, and monitor results.
2. **Agent scraper** — pulls the journey blocks (converted from Sierra's
   Lexical editor state to Markdown), the 17 tools, and all 197 KB articles
   with their Markdown bodies.
3. **Classifier** — for each session, sends the transcript + agent context to
   Sonnet 4.6 (with Anthropic prompt caching) and receives a structured
   verdict: category, severity, pain points, suggestion, related journey
   blocks and KB articles.
4. **Analyzer** — aggregates classifications and generates
   `reports/transaction_status_improvements.md`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in real values
```

Required `.env` keys:

| Key | Where to get it |
|-----|-----------------|
| `SIERRA_COOKIE` | Chrome DevTools → Network → any `graphql` request → Request Headers → `cookie` |
| `SIERRA_CSRF_TOKEN` | Same Headers panel → `x-sierra-csrf-token` |
| `ANTHROPIC_API_KEY` | `https://console.anthropic.com` |
| `SSL_VERIFY` | `false` behind a corporate TLS-inspection proxy |

Cookies expire when you log out of Sierra. When the scraper returns HTTP 401,
refresh your browser session and re-copy them.

## Running

```bash
# Initial discovery (once, from a captured HAR):
python scripts/parse_har.py        # summarises every GraphQL op in the HAR
python scripts/extract_queries.py  # writes src/queries.py

# Day-to-day scraping:
python scripts/scrape_agent.py                       # ~30s, refreshes journeys+tools+KB
python scripts/scrape_sessions.py --today --sample 100  # ~5 min, picks 100 stratified

# Classification + report:
python -m src.sierra_classifier --limit 100          # ~3 min, classifies new sessions
python scripts/analyze_transaction_status.py         # writes reports/…md

# Exploratory:
python scripts/stats.py                              # no-LLM stats over the DB
```

## Project layout

```
src/
  config.py            loads .env + shared paths
  sierra_client.py     GraphQL client (cookie + CSRF, retry, pacing)
  queries.py           auto-generated query strings from the HAR
  lexical_md.py        converts Sierra's Lexical JSON to Markdown
  sierra_db.py         SQLite schema (13 tables)
  sierra_classifier.py Sonnet 4.6 classifier with prompt caching

scripts/
  parse_har.py                  HAR → data/graphql_ops/ (one JSON per op)
  extract_queries.py            selects the ops we need → src/queries.py
  smoke_test.py                 3 GraphQL calls, checks auth + endpoints
  scrape_sessions.py            conversations + transcripts + traces + monitors
  scrape_agent.py               journeys + tools + KB articles
  stats.py                      quick exploratory stats (no LLM)
  analyze_transaction_status.py builds the final markdown report

data/
  sierra.db               SQLite — all scraped data
  sierra_capture.har      initial browser HAR (gitignored)
  graphql_ops/            one JSON per distinct captured GraphQL op

reports/
  transaction_status_improvements.md   generated
```

## DB schema at a glance

```
sessions ─┬─ session_tags
          ├─ session_journeys
          ├─ session_details
          ├─ messages
          ├─ traces
          ├─ monitor_results
          └─ classifications

journeys ── journey_blocks
tools
kb_sources ── kb_articles
```
