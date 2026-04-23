# Deploy to Streamlit Community Cloud

## Pre-flight checklist

- [ ] GitHub repo created (recommend **private** — the DB contains caller PII)
- [ ] `data/sierra.db` decision: anonymize OR include in private repo via `git add -f data/sierra.db`
- [ ] Streamlit Cloud account linked to your GitHub
- [ ] Passcode chosen (default fallback `12345`, override via secrets)

## Step 1 · Initialize git + push

```bash
cd "c:/Users/jaguilar/OneDrive - Euronet Worldwide/Desktop/Claude Code/Sierra scrapping"

git init
git add .
# By design .gitignore excludes data/*.db — if you want to include the DB
# in your private repo so Streamlit Cloud can read it, force-add:
git add -f data/sierra.db
git add -f reports/issue_log_raw.json   # needed by the dashboard too

git commit -m "initial dashboard for deploy"
git branch -M main
git remote add origin https://github.com/<your-user>/<repo-name>.git
git push -u origin main
```

## Step 2 · Connect on Streamlit Cloud

1. Go to https://share.streamlit.io
2. Click **New app**
3. Select the repo, branch `main`, main file path `dashboard.py`
4. Click **Advanced settings** → **Secrets** and paste:

```toml
APP_PASSCODE = "12345"

# Only needed if you run the scrapers from the cloud.
# Leave blank if the cloud app is read-only against a pre-scraped DB.
SIERRA_COOKIE      = ""
SIERRA_CSRF_TOKEN  = ""
ANTHROPIC_API_KEY  = ""
```

5. Click **Deploy**.  First build takes ~5 min (installs pandas, plotly, anthropic).

## Step 3 · Test

- Open the deployed URL
- Passcode gate appears → enter `12345` → dashboard loads
- Navigate all pages (Overview, Gap Proposals, Glossary, Investigate, Simulations)
- Toggle ES/EN to verify translations

## Rotating the passcode

In Streamlit Cloud UI → your app → **Settings → Secrets** → edit `APP_PASSCODE`.
No redeploy needed — change takes effect after next reload.

## Common gotchas

- **`st.secrets` KeyError** → you didn't paste the secrets block. Default passcode 12345 still works.
- **DB not found** → you didn't force-add `data/sierra.db`. Either add it (private repo), or generate an anonymized version.
- **`ModuleNotFoundError: src`** → Streamlit Cloud needs `dashboard.py` at repo root so it can find `src/` next to it. Confirm root-level `dashboard.py`.
- **Sierra credentials errors** → the dashboard doesn't need them for read-only mode. If you see these errors, a scraper is being triggered — check the code for accidental imports.

## After deploy

You'll get a public URL like `https://<app-name>.streamlit.app` guarded by the passcode. Share with your team; anyone without the code cannot see the data.
