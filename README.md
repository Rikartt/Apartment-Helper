# WGPendler

A WG (shared-flat) room hunting assistant for ETH Zürich students. It ingests room listings from several Swiss housing sites, extracts structured details with Claude, scores each listing by fit and reply likelihood, and shows everything on a Zürich commute map.

## Stack

- **Backend:** FastAPI + SQLite
- **Frontend:** single-page Leaflet map (`frontend/index.html`), served by the backend
- **Sources:** Flatfox, WGZimmer, Ronorp, Homegate (scraped via `httpx`/Playwright)
- **AI:** Anthropic API for listing extraction and top-match analysis

## Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
# 1. Clone the repo
git clone git@github.com:Rikartt/Apartment-Helper.git
cd Apartment-Helper

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright's browser binaries (used by some ingestion adapters)
playwright install chromium

# 5. Configure your API key
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
# (the key goes in .env, NOT .env.example — .env is git-ignored)
```

### macOS notes

- Tested on Apple Silicon with Python 3.11–3.14 from python.org.
- `playwright install chromium` is required — the WGZimmer and Ronorp
  adapters launch a headless browser. Without it those two sources are
  skipped (Flatfox and Homegate still work).
- The first ingest now runs on a background thread, so the server answers
  on http://localhost:8000 immediately while listings load over the next
  minute or two.

## Running locally

Make sure the virtual environment from step 2 is activated first — if it isn't,
your shell will fall back to a system-wide or Homebrew `uvicorn` that doesn't
have this project's dependencies installed, and you'll get
`ModuleNotFoundError: No module named 'fastapi'`.

```bash
source .venv/bin/activate   # Windows: .venv\Scripts\activate
# your prompt should now be prefixed with (.venv) — that's how you know it's active
uvicorn backend.main:app --reload
```

Activation doesn't persist across terminal tabs/sessions, so re-run it any time
you open a new shell before starting the server. If you'd rather not activate,
call the venv's binary directly instead: `.venv/bin/uvicorn backend.main:app --reload`.

Then open **http://localhost:8000** in your browser.

On startup the app initializes the SQLite database (`wgpendler.db`) and automatically ingests listings from all sources — this can take a minute or two the first time.

## Usage

- **Extract** — click the "Extract" button to run Claude over newly ingested listings and pull out structured details (requirements, vibe, red flags, etc.)
- **Filters** — narrow by max rent, max commute time, campus (Zentrum/Hönggerberg), and mode (transit/bike)
- **Top 5** — the amber button surfaces your best-fit listings with a short Claude-generated analysis per listing
- **Profile** — edit your applicant profile (used for scoring and analysis) in the Profile panel; it's persisted to `profile.json`

To re-ingest listings manually without restarting the server:

```bash
curl -X POST "http://localhost:8000/ingest?sources=flatfox,wgzimmer,ronorp,homegate"
```

## Project structure

```
backend/
  main.py              FastAPI app and routes
  db.py                SQLite schema and queries
  ingest.py            Ingestion orchestration
  ingestion/           One adapter per source
  extraction.py        Claude-based listing extraction
  scoring.py           Fit score and reply-likelihood scoring
  commute.py           Commute time estimation
frontend/
  index.html           Map UI, served at "/"
profile.json            Your applicant profile (edited via the UI)
```
