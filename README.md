# WGPendler

A room-hunting assistant for ETH Zürich students. It pulls listings from several Swiss housing sites, scores each one for how well it fits you and how likely the landlord is to reply, shows them on a commute map, and drafts the first message for you. It never contacts anyone on its own: you read, edit and send every message yourself.

Two modes:

- **Solo**: WG rooms for you alone.
- **Group**: whole flats for you and some classmates.

## Setup

Requires Python 3.11+ and (optionally, for Extract / Top 5 / Draft) an [Anthropic API key](https://console.anthropic.com/). Without a key, browsing, filtering and rule-based scoring all still work.

```bash
git clone <repo-url> WGHelfer && cd WGHelfer

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium      # only needed if you enable the Ronorp adapter

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...   (.env is git-ignored; never put the key in .env.example)
```

## Running

Activate the venv first. If you don't, your shell may pick up a system or Homebrew `uvicorn` without this project's dependencies and you get `ModuleNotFoundError: No module named 'fastapi'`.

```bash
source .venv/bin/activate        # your prompt should now start with (.venv)
uvicorn backend.main:app --reload
```

Activation does not persist across terminal tabs; repeat it in each new shell, or call `.venv/bin/uvicorn backend.main:app --reload` directly. Then open **http://localhost:8000** (add `--port 8110` to use another port).

**First run:** the database (`wgpendler.db`) starts empty and the server ingests all sources on a background thread. The page loads immediately and shows "syncing sources...". In a fresh-clone test the full first ingest took about 8 minutes (Flatfox is the slow part, around 5-6 minutes; Homegate about 2). Listings appear in the UI as they are stored, so refresh after a few minutes. Later startups skip ingesting if the last successful run is younger than the refresh interval.

## Features

### Scoring
- **Fit score**: how well the listing matches your profile (rent, commute to both campuses, location, move-in date, house rules, languages, wants). Computed locally, no API cost.
- **Reply score**: how likely you are to get an answer (ad age, private vs agency, what the ad asks for).
- **Dealbreakers**: hard no's (over budget, implausible price, "no students", too far, etc.). Dealbreaker listings are marked and can be hidden with the "Hide dealbreakers" filter; they are never suggested in Top 5.
- Edit your profile in the **Profile** panel; it is saved to `profile.json`.

### Claude actions and their cost
Only these three call the Anthropic API. Costs are small but real; the default models are cheap ones for bulk work.

| Action | What it does | Cost |
|---|---|---|
| **Extract** (menu, top right) | Reads the ad text of up to 50 not-yet-extracted listings (newest first, only ones that pass rent and commute prefilters) and pulls out requirements, vibe, red flags. One small Haiku-class call per listing. | Lowest, per listing |
| **Top 5** | Analyses your five best non-dealbreaker listings (strengths/weaknesses). One call, cached per set of top-5 ids, so reloading is free. | One small call |
| **Draft** (in a listing's detail view) | Writes a first message in the ad's language from your template and profile. Sonnet-class model. In group mode it writes from the group's members and notes only (nothing invented). | One medium call per draft |

If the key is missing or the credit is exhausted the UI shows a clear error instead of failing silently. Auto-extraction after each ingest is on by default when a key is set (see `WGP_AUTO_EXTRACT`); set `WGP_AUTO_EXTRACT=0` to spend nothing until you press Extract.

### Commute times
- **Transit** minutes are real timetable connections from [transport.opendata.ch](https://transport.opendata.ch) (free, rate-limited): nearest stops to the listing, median of the connections arriving at ETH Zentrum (Zürich, ETH/Universitätsspital) or ETH Hönggerberg around 08:00 on the next weekday, plus the walk to the first stop. Results are cached in SQLite (`commute_cache`) per ~100 m grid cell and campus for 90 days.
- Lookups run in a background thread after each ingest (throttled to 2 requests/s, backing off on HTTP 429), never in a request handler. Listings the incremental ingest does not re-fetch are upgraded from the cache right after the run, so they need no extra API calls when a neighbour in the same cell was already looked up. Until a listing is looked up, or if the API is down, it shows a straight-line estimate with a `~` prefix (`commute_source` is `estimate`; `timetable` otherwise).
- **Bike** minutes remain an estimate (15 km/h straight-line) plus a climb penalty of up to 5 min for Hönggerberg, which sits about 100 m above the city.
- Backfill existing rows (resumable): `python -m backend.commute [--max-cells N]`. Roughly 5 API calls per new grid cell; a full database takes a while at the polite rate.

### Tracking
Mark each listing **Sent**, **Replied** or **Skipped**. The default "To do" view hides what you've handled. Status survives re-ingests and is never changed by the app, even when a listing disappears.

**Reply stats** (menu ⋯ > Reply stats, `/stats.html`): your reply rate by source, message length, rent, how soon you wrote, fit and commute. An outcome is a listing marked Replied, or one still marked Sent 7 days after your first message. Once there are `WGP_LEARN_MIN_OUTCOMES` (20) outcomes, a small model fitted to them is blended into the reply score (more weight as outcomes grow, at most 70%); the page shows what it learned. It retrains when you change a status or log a sent message, and when the stats page finds new outcomes. Weights are stored in the `learned_weights` table.

### Draft to reply loop
- **Draft checklist**: under a draft, a checklist of what the ad asked for (and its hard requirements). A keyword/date/number heuristic ticks what your text already covers; the rest get an amber hint. It runs in the browser, costs nothing and never edits your text.
- **Sent history**: Mark as sent from the draft panel (or after Copy & open ad) saves the final text and time; the detail view shows "You sent this on ...".
- **Follow-ups**: a listing still marked Sent after `WGP_FOLLOWUP_DAYS` days (default 3) gets a "Follow up?" chip and appears under Show > Follow-ups. "Draft follow-up" asks Claude for a short, polite note in the ad's language that does not repeat your first message (one small API call; in group mode it uses `/group/draft/{id}?followup=1`). You still send it yourself.
- **Copy & open ad**: copies the draft, opens the ad in a new tab and asks "Mark as sent?".

### Fresh listings
- The server re-ingests in the background every `WGP_INGEST_INTERVAL_MIN` minutes (default 45, jittered).
- Listings first seen since your last visit get a **NEW** badge, there is a **New only** toggle, and a notice appears when a background ingest adds listings.
- A **Message today** strip highlights the best listings you haven't contacted yet.
- **Gone listings**: when a source stops returning a listing it is marked gone and hidden (`GET /listings?include_gone=1` shows them). To avoid false alarms, a listing is only marked gone after two consecutive healthy runs, and runs that returned under 50% of the previous count or were truncated are ignored.

### Group mode
Switch with the **Solo | Group** toggle. Group mode shows whole flats (not WG rooms), scored per person: each member has a campus, and the worst-case commute counts. Edit group size, members and shared notes in the group editor. Per-person pills show how each member fares; group drafts use only the facts you entered.

Each member may carry their own monthly `budget_chf` (a field in the group editor; empty means the group budget). Rent is split evenly, or by room size when every seat has a member with `room_m2` (set via `PUT /profile`) and the ad gives living area and enough bedrooms (each pays their room plus an equal share of the common area). A flat is a dealbreaker if any member would pay more than 15% over their own budget; the member is named in the dealbreakers, and smaller overruns appear in the reasons. `POST /group/draft/{id}?followup=1` drafts a short follow-up to the stored first message (409 if none is stored); it never repeats the first message and adds no new facts.

### Keyboard shortcuts
Press `?` in the app for the list.

| Key | Action |
|---|---|
| `j` / `k` (or arrows) | Next / previous listing |
| `Enter` | Open selected listing |
| `s` | Mark Sent |
| `x` | Mark Skipped |
| `o` | Open the original ad |
| `d` | Draft a message (in detail view) |
| `?` | Show shortcuts |
| `Esc` | Close panel / detail |

### Use it on your phone (PWA)

The app is installable and works well on a phone. On the same Wi-Fi as your Mac:

1. Start the server so it listens on the network, not just localhost: `.venv/bin/uvicorn backend.main:app --host 0.0.0.0`
2. Find the Mac's LAN IP: `ipconfig getifaddr en0` (Wi-Fi; try `en1` if empty), or System Settings > Wi-Fi > Details.
3. On the phone open `http://<that-ip>:8000`, then **Add to Home Screen** (Safari: Share menu; Chrome: menu > Install app). It opens full screen with the red-on-black WG icon.

Notes:
- The service worker caches only the app shell (page and Leaflet files). Listings and all other API data are always fetched live and never cached, so you never see stale listings; offline you get a red "OFFLINE" banner instead. Some browsers only allow installing from HTTPS or localhost; Android Chrome may offer "Add to Home screen" as a plain shortcut over http, which still works.
- **Security:** with `--host 0.0.0.0` anyone on that network can open the app, read your profile and trigger actions (including Claude calls that spend your credit). Only do this on a network you trust, not on public or shared Wi-Fi.
- Optional guard: set `WGP_ACCESS_TOKEN=some-long-random-string` in `.env`. Every request then needs it: open `http://<ip>:8000/?token=some-long-random-string` once on the phone; the server sets a cookie and redirects, and you are in. It is off by default. This is a simple shared secret over plain HTTP, not real authentication; use a VPN or HTTPS if you need more.
- Regenerate the icons with `python tools/make_icons.py` (standard library only).

### Phone over HTTPS (optional)
Plain `http://<ip>:8000` works, but some browsers only fully install the PWA from HTTPS, and the access token travels unencrypted. Preferred fix: Tailscale, which gives real HTTPS on your private tailnet.
1. Install Tailscale on the Mac and the phone, sign in to the same tailnet, and enable HTTPS certificates in the admin console (DNS).
2. Start the app on localhost as usual (no `--host 0.0.0.0` needed), then run `scripts/phone-https.sh` (wraps `tailscale serve --bg 8000`; `stop` and `status` also work).
3. Open the printed `https://<machine>.<tailnet>.ts.net` URL on the phone and Add to Home Screen.

Alternative: `mkcert` (local CA). Run `mkcert -install`, `mkcert <lan-ip>`, start uvicorn with `--ssl-keyfile` and `--ssl-certfile`, and install mkcert's root CA on the phone (iOS also needs it enabled under Certificate Trust Settings). More fiddly, and LAN-only.

The app depends on neither. `WGP_ACCESS_TOKEN` still works the same: open `https://<url>/?token=...` once; over HTTPS the token and cookie are encrypted in transit. Keep it set if other devices share your tailnet, since Tailscale's HTTPS protects the transport, not who on the tailnet can reach the app.

### Database backups
Once a day (and on startup if today's is missing) the app copies `wgpendler.db` to `backups/wgpendler-YYYY-MM-DD.db` using SQLite's backup API, which is safe while the app runs. The newest `WGP_BACKUP_KEEP` (default 7) are kept; `backups/` is git-ignored. `GET /backup/status` shows the state, `POST /backup/now` forces one.

Restore: stop the app, then `cp backups/wgpendler-YYYY-MM-DD.db wgpendler.db` and delete `wgpendler.db-wal` and `wgpendler.db-shm` if present, then start the app again.

### Phone alerts (optional)
After each ingest, the app can send one digest (max 5 listings) of new listings whose fit is at or above `WGP_NOTIFY_MIN_FIT`. Alerts go only to you, never to landlords. Nothing is sent unless you configure a channel.

ntfy (easiest):
1. Install the ntfy app on your phone.
2. Subscribe to a long random topic name (it acts as a password, e.g. `wgp-k3j9x2q8m4v7`).
3. Put it in `.env`: `WGP_NTFY_TOPIC=wgp-k3j9x2q8m4v7`
4. Restart the server, then test: `curl -X POST localhost:8000/notify/test`

Telegram (optional): create a bot with @BotFather, get your chat id from @userinfobot, set `WGP_TELEGRAM_TOKEN` and `WGP_TELEGRAM_CHAT_ID`.

Endpoints: `GET /notify/status` (configured channels, threshold, quiet hours, last error) and `POST /notify/test` (sends a test message).

## Environment variables

All are optional except the API key if you want Claude features. Set them in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Enables Extract, Top 5, Draft |
| `WGP_AUTO_EXTRACT` | `1` | `0` stops automatic extraction after ingests |
| `WGP_AUTO_EXTRACT_LIMIT` | `25` | Max listings auto-extracted per ingest |
| `WGP_EXTRACT_MAX_PER_RUN` | `50` | Default batch size for the Extract button |
| `WGP_EXTRACT_MAX_TRANSIT_MIN` | `50` | Skip extracting listings with a longer transit commute |
| `WGP_EXTRACT_MODEL` | `claude-haiku-5-5` | Model for extraction |
| `WGP_BEST_MODEL` | same as extract model | Model for Top 5 |
| `WGP_DRAFT_MODEL` | `claude-sonnet-5-5` | Model for drafts |
| `WGP_BACKUP_KEEP` | `7` | Daily DB backups to keep |
| `WGP_BACKUP_DIR` | `backups/` in repo root | Backup folder |
| `WGP_ACCESS_TOKEN` | none (off) | Shared secret for phone/LAN access, see "Use it on your phone" |
| `WGP_ENABLE_RONORP` | `0` | `1` adds Ronorp back to the default sources |
| `WGP_INGEST_INTERVAL_MIN` | `45` | Minutes between background ingests; `0` = off |
| `WGPENDLER_INCLUDE_APARTMENTS` | `1` | `0` stops Flatfox/Homegate from collecting whole flats (needed for group mode) |
| `WGP_FULL_REFRESH_HOURS` | `24` | Flatfox/Homegate re-fetch every ad's details at most this often; runs in between only fetch new ads and ads whose price changed. `0` = every run is a full refresh |
| `WGPENDLER_GEOCACHE` | `geocache.sqlite` in repo root | Path of the address-to-coordinates cache |
| `WGP_NTFY_TOPIC` | none | ntfy topic for phone alerts |
| `WGP_NTFY_SERVER` | `https://ntfy.sh` | ntfy server |
| `WGP_TELEGRAM_TOKEN`, `WGP_TELEGRAM_CHAT_ID` | none | Telegram alerts (both needed) |
| `WGP_NOTIFY_MIN_FIT` | `75` | Minimum fit score to alert on |
| `WGP_NOTIFY_QUIET` | `23-07` (set it empty to disable) | Quiet hours (local time); digests are held |
| `WGP_LEARN_MIN_OUTCOMES` | `20` | Outcomes (replies, or sends with no reply after 7 days) needed before the reply score starts learning from them |
| `WGP_LIVE_API`, `E2E_INGEST_WAIT`, `RUN_E2E` | see Tests | Only used by the e2e script |

## Sources

| Source | Status |
|---|---|
| Flatfox | Used (public API, tiled to get past its result cap) |
| Homegate | Used (up to 40 result pages per run) |
| WOKO | Used (student housing cooperative; few listings) |
| Ronorp | Off by default: as of 2026-10-18 its Zürich WG-room category is genuinely empty ("Es sind noch keine Inhalte vorhanden", its API reports 0 posts), so there is nothing to fetch. The adapter detects this and returns nothing in a few seconds. Set `WGP_ENABLE_RONORP=1` to try it again later. First results page only (robots.txt disallows paging) |
| WGZimmer | Not usable: search is behind reCAPTCHA, which we don't bypass. The adapter is present but returns nothing, and logs "no listings returned" |
| students.ch and others | Not implemented: no permitted automated access (login/captcha walls or terms of service). The app only reads what robots.txt and the site allow, at about 1 request per second |

### Incremental ingest and listing dates

Flatfox and Homegate keep a per-source record of ads whose details were already fetched, with their price (tables `source_known`, `source_state`). A periodic run fetches the cheap list (Flatfox pin tiles, Homegate result pages) every time, but only fetches/geocodes new ads and ads whose price changed. Ads present in the list but not re-fetched still count as seen, so gone detection works as before. A full refresh happens at most every `WGP_FULL_REFRESH_HOURS` (edits that leave the price unchanged show up then).

`listed_at` (stored as UTC `YYYY-MM-DD HH:MM:SS`, used for recency) per source:

| Source | Date available | Field |
|---|---|---|
| Flatfox | Yes, publication time | `published` (fallback `created`) of the public-listing API |
| Homegate | Ad creation time (not a re-publication date, so an old ad that is still live looks old) | `meta.createdAt` in the results page's embedded `window.__INITIAL_STATE__` JSON |
| WOKO | No per-object date (the only timestamps on the pages are page-level CMS times) | none, falls back to first-seen |
| WGZimmer, Ronorp | Not usable / empty source | none |

Homegate's embedded JSON also carries coordinates (accuracy HIGH/MEDIUM), which are used instead of geocoding. Other addresses go through the cache, then swisstopo with 4 parallel workers (paced), then Nominatim at most 1 request per second.

Ingest errors for one source never stop the others; see `GET /ingest/status` for the last 5 runs. To re-run manually:

```bash
curl -X POST "http://localhost:8000/ingest?sources=flatfox,homegate"
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is offline (saved HTML/JSON fixtures, no network, no API calls). There is also an opt-in end-to-end smoke test that starts a real server, ingests, and checks the API and page:

```bash
RUN_E2E=1 python tests/smoke_e2e.py              # no Anthropic calls
RUN_E2E=1 WGP_LIVE_API=1 python tests/smoke_e2e.py   # also exercises Claude (costs credit)
```

`E2E_INGEST_WAIT` (default 240 seconds) is how long it waits for the first ingest.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'fastapi'`**: the venv is not active, see Running.
- **Empty list right after first start**: the first ingest takes several minutes; check `curl localhost:8000/ingest/status`.
- **Ronorp returns nothing**: expected, the category is empty on the site and Ronorp is off by default (see Sources). If you enabled it and it should have listings, run `playwright install chromium`. WGZimmer always returns nothing, by design.
- **"credit balance exhausted" / auth errors**: top up billing or fix `ANTHROPIC_API_KEY` in `.env`, then restart.
- **Address already in use**: another server is on port 8000; use `--port`.
- **Map tiles missing**: tiles come from Esri; check your connection.
- **Start over**: stop the server and delete `wgpendler.db` (listings and your Sent/Replied marks) and optionally `geocache.sqlite`.

## Project structure

```
backend/   main.py (routes), db.py, ingest.py, ingestion/ (one adapter per source),
           scoring.py, extraction.py, drafting.py, group.py, notify.py, llm.py, commute.py
frontend/  index.html (Leaflet map UI, served at "/")
tests/     offline pytest suite + opt-in smoke_e2e.py
profile.json   your applicant profile (edited in the UI)
```
