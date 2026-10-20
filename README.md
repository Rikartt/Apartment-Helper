# WGPendler

A room-hunting assistant for ETH Zürich students. It pulls listings from Swiss housing sites, scores each one for how well it fits you and how likely the landlord is to reply, shows them on a commute map, and drafts the first message for you. It never contacts anyone on its own: you read, edit and send every message yourself.

Two modes:

- **Solo**: WG rooms (and optionally studios) for you alone.
- **Group**: whole flats for you and some classmates.

## Setup

Requires Python 3.11+. An [Anthropic API key](https://console.anthropic.com/) is optional: it is only needed for Extract, the Top 5 analysis, drafts and ad summaries. Without a key, browsing, filtering and all scoring still work.

```bash
git clone https://github.com/Rikartt/Apartment-Helper.git && cd Apartment-Helper

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # only needed if you enable the Ronorp adapter

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...   (.env is git-ignored; never put the key in .env.example)
```

## Running

```bash
source .venv/bin/activate        # your prompt should now start with (.venv)
uvicorn backend.main:app --reload
```

Then open **http://localhost:8000** (add `--port 8110` for another port).

Activate the venv in every new terminal tab, or call `.venv/bin/uvicorn backend.main:app --reload` directly. Without it your shell may pick up a system or Homebrew `uvicorn` and fail with `ModuleNotFoundError: No module named 'fastapi'`.

**First run:** the database (`wgpendler.db`) starts empty and the server ingests all sources in the background. The page loads at once and the header shows sync progress ("syncing 2/5 · homegate" with a thin bar). Listings appear as they are stored; refresh after a few minutes. The first ingest is slow because it fetches every ad's details (about 8 minutes in a fresh-clone test, mostly Flatfox). Later runs are incremental and much faster. A restart skips the startup ingest if the last successful run is younger than the ingest interval.

A short walkthrough (profile, then group and phone alerts, both optional) shows while your profile has no message template. Once skipped or finished, it does not come back.

## Using it day to day

- **Update the app**: stop the server (Ctrl+C), then `git pull`, `pip install -r requirements.txt` (only does something if dependencies changed) and start it again as in Running. The phone app picks up the new version on its next open; a page left open shows a "WGPendler was updated. Reload" notice. You never need to clear site data. `profile.json` is tracked in git, so if `git pull` refuses because you edited your profile, commit your profile first.
- **Filters**: rent, commute, campus and transit/bike are always visible. **More filters** has type, poster, New only, Hide dealbreakers, Show low-trust, Hide priority/premium and Hide befristet. **✎ Draw area** on the map limits everything to a part of town. **reset** clears them all.
- **Top 5** (solo only): the five best listings for your profile, with a short Claude analysis of each. It respects Hide priority/premium, Hide befristet and the drawn area, but not the rent slider, the Show tab or New only.
- **Message today**: a strip of up to five fresh, strong listings you have not acted on yet. It follows your filters but not the Show tab or New only.
- **Drafts**: open a listing, press **Draft**, edit the text, then **Copy & open ad** and send it yourself on the site. Then mark it **Sent**. Nothing is ever sent automatically.
- **Status**: mark listings **Sent**, **Replied** or **Skipped**. "To do" hides what you've handled; "Follow-ups" shows sent ones with no reply after a few days.
- **Phone alerts** (optional): one ntfy or Telegram digest after an ingest when good new listings arrive. They go only to you. See Phone alerts.

## Features

### Scoring
- **Fit score**: how well the listing matches your profile (rent, commute to both campuses, location, move-in date, house rules, languages, wants). Computed locally, no API cost.
- **Reply score**: how likely you are to get an answer (ad age, private vs agency, what the ad asks for). Once you have enough outcomes it also learns from your own replies (see Tracking).
- **Dealbreakers**: hard no's: over budget, implausible price, available too late (more than 120 days after your move-in date), or a requirement in the ad you don't meet ("no students", an age range, …). If your profile wants a long-term room, a befristet listing is a dealbreaker too. A befristet listing whose stated end date ("bis 30.09.2026") has passed is always a dealbreaker ("Rental period already over"). Dealbreakers are marked, can be hidden with "Hide dealbreakers", and never appear in Top 5, Message today or phone alerts.
- **Move-in date**: if the ad's date field is implausible (more than 60 days before the ad was published, e.g. 2004-11-01), the app scores on the date stated in the ad's text ("ab 1. November", "per sofort") and marks it with `*`.
- Edit your profile in the **Profile** panel; it is saved to `profile.json`.

### Trust score (scam check)
Each listing gets a 0-100 trust score, computed locally, with a reason for every deduction. Signals: price far below comparable listings nearby; scam phrases such as payment before viewing, keys by post, "I am abroad" or WhatsApp-only contact (DE/EN/FR/IT); the same text at another address or price; no house number or no photos; agency poster; red flags from Extract. High is 75+, medium 50-74, low under 50. A shield chip shows the score. Low-trust listings are hidden by default ("Show low-trust" in More filters), get a warning above the draft button, and are left out of phone alerts, Top 5 and Message today.

### Map, paid placements and befristet
- **Commute rings**: the dashed ~10/20/30 min rings around the selected campus use the straight-line estimate.
- **✎ Draw area** (top right of the map): click or tap the corners of an area, then close it on the first corner, with a double-click or with Done (Esc or Cancel aborts). The list, pins, Message today and Top 5 follow it. It counts in "More filters (n)" and is remembered in this browser until **✕ Clear area** or **reset**. Phone alerts do not use it.
- **Paid placements**: listings a source marks as paid or prioritised (Flatfox Priority, premium/top ads) get a purple PRIORITY or PREMIUM chip. Flatfox Priority means only paying Priority members can apply until the date on the chip; after that the listing counts as normal. "Hide priority/premium" hides all kinds at once.
- **Befristet (fixed-term) lets** get a grey BEFRISTET chip; "Hide befristet" hides them. Flatfox flags these itself. For other sources the app looks for wording like "befristet" (not "unbefristet"), "Zwischenmiete", "temporär" or "sublet", or uses the duration from Extract.
- Both switches are remembered in the browser and also saved on the server, so phone alerts follow them.

### Photos
The detail view shows up to 12 of the ad's photos as a sideways-scrolling strip. Tap one (or focus it and press Enter) for a full-size viewer: ‹ › or the arrow keys (swipe on a phone) step through, Esc or ✕ closes it. List cards stay text-only so the list is light on a phone. Images load straight from the source (Flatfox, Homegate) only when you open a detail; the app stores only their URLs and never caches them. WOKO, Ronorp and WGZimmer give no photos. Listings stored before photos existed get them on the next Flatfox full refresh (`WGP_FULL_REFRESH_HOURS`, default daily).

### Claude actions and their cost
Only these four call the Anthropic API. Costs are small but real.

| Action | What it does | Cost |
|---|---|---|
| **Extract ads** (⋯ menu, solo) | Reads up to 50 not-yet-extracted ads (newest first, only ones within your budget and the transit limit) and pulls out requirements, vibe and red flags. | One small call per listing |
| **Top 5** (solo) | Picks your five best listings by fit and reply score, leaving out dealbreakers, low-trust ads and whole flats, after applying Hide priority/premium, Hide befristet and the drawn area. Claude then lists strengths, weaknesses and a tip for each. The analysis is cached for the last 8 sets of picks, so reloading or toggling a filter back is free. Without a key you still get the five picks, without analysis. | One small call per new set |
| **Draft** (detail view) | Writes a first message from your template and profile. Tone toggle: Warm (default, 4-7 sentences) or Short (2-3; 3-4 in group mode). When the ad's language (German or English) differs from your strongest language, a toggle lets you write in yours. Changing an option regenerates (it asks first if you edited the text). Group drafts use only the members and notes you entered. | One medium call per draft |
| **Ad summary** (automatic, detail view) | Opening an extracted non-English ad in solo mode shows a one-line English summary under the title. Cached per listing. Hidden on any error. | One small call per ad, once |

If the key is missing or the credit is used up, the UI shows a clear error. After each ingest up to 25 new ads are extracted automatically when a key is set; set `WGP_AUTO_EXTRACT=0` to spend nothing until you press Extract.

API: `POST /draft/{id}` and `POST /group/draft/{id}` take `?tone=warm|short&lang=auto|de|en` (other values: 422). `?followup=1` drafts a follow-up instead and ignores tone and language.

### Commute times
- **Transit** minutes are real timetable connections from [transport.opendata.ch](https://transport.opendata.ch) (free, rate-limited): the median connection arriving at ETH Zentrum or ETH Hönggerberg around 08:00 on the next weekday, plus the walk to the first stop. Results are cached in SQLite (`commute_cache`) per ~100 m grid cell and campus for 90 days.
- Lookups run in a background thread after each ingest (at most 2 requests/s, backing off on HTTP 429). Until a listing is looked up, or if the API is down, it shows a straight-line estimate (22 km/h) with a `~` prefix.
- Areas with no stop within about 25 minutes' walk, or no connection, keep the estimate and are retried after 7 days.
- **Bike** minutes are always an estimate (15 km/h straight-line) plus up to 5 min for the climb to Hönggerberg.
- Backfill existing rows (resumable): `python -m backend.commute [--max-cells N]`. About 5 API calls per new grid cell.

### Tracking and reply stats
Mark each listing **Sent**, **Replied** or **Skipped** (buttons on the card, or `s`/`x`). Status survives re-ingests and is never changed by the app, even when a listing disappears.

**Reply stats** (⋯ menu > Reply stats, `/stats.html`): your reply rate by source, message length, rent, how soon you wrote, fit and commute, plus median reply time. An outcome is a listing marked Replied, or one still marked Sent 7 days after your first message. From 20 outcomes (`WGP_LEARN_MIN_OUTCOMES`) a small model fitted to them is blended into the reply score (more weight as outcomes grow, at most 70%); the page shows what it learned. It retrains in the background about 1.5 s after your last status change, so the buttons answer at once.

### Draft to reply loop
- **Draft checklist**: under a draft, a checklist of what the ad asked for. A local heuristic ticks what your text already covers; the rest get an amber hint. It costs nothing and never edits your text.
- **Copy & open ad**: copies the draft, opens the ad in a new tab and asks "Mark as sent?". Marking as sent saves the final text and time; the detail view then shows "You sent this on ...".
- **Follow-ups**: a listing still marked Sent after `WGP_FOLLOWUP_DAYS` days (default 3) gets a "Follow up?" chip and appears under Show > Follow-ups. "Draft follow-up" writes a short, polite note in the language of your first message, without repeating it. You still send it yourself.

### Fresh listings
- The server re-ingests in the background every `WGP_INGEST_INTERVAL_MIN` minutes (default 45, jittered by ±15%).
- Listings first seen since your last visit get a **NEW** badge, **New only** shows just those, and a notice appears when a background ingest adds listings.
- **Gone listings**: when a source stops returning a listing it is hidden (`GET /listings?include_gone=1` still shows them). To avoid false alarms, a listing is only marked gone after two healthy runs in a row; runs that returned under 50% of the previous count, or were cut short, are ignored.

### Group mode
Switch with the **Solo | Group** toggle. Group mode shows whole flats, scored per person: each member has a campus, and the worst commute counts. Edit group size, members and shared notes in the group editor. Per-person pills show how each member fares; minus points show as amber "!" chips.

Each member may have their own monthly budget (empty means the group budget). Rent is split evenly, or by room size when every member has a `room_m2` (set via `PUT /profile`) and the ad gives living area and enough bedrooms. A flat is a dealbreaker if any member would pay more than 15% over their budget; smaller overruns appear in the reasons. Group drafts and follow-ups use only the facts you entered. Top 5 is not available in group mode.

### Keyboard shortcuts
Press `?` in the app for the list.

| Key | Action |
|---|---|
| `j` / `k` (or ↓ / ↑) | Next / previous listing |
| `Enter` (or `Space` on a card) | Open the selected listing |
| `s` | Mark Sent |
| `x` | Mark Skipped |
| `o` | Open the original ad |
| `d` | Draft a message (in detail view) |
| `?` | Show shortcuts |
| `Esc` | Close the menu, window, panel or detail and return focus to where you were. In the draft box the first `Esc` only leaves the box; while drawing an area it cancels the drawing |

The UI works with a keyboard and screen reader (focus stays inside open windows, landmarks and labels are set) and aims for WCAG AA contrast. On a phone the main buttons are at least 40 px tall.

## Phone

### Use it on your phone (PWA)
The app is installable. On the same Wi-Fi as your MacBook:

1. Start the server so it listens on the network: `.venv/bin/uvicorn backend.main:app --host 0.0.0.0`
2. Find the Mac's LAN IP: `ipconfig getifaddr en0` (try `en1` if empty), or System Settings > Wi-Fi > Details.
3. On the phone open `http://<that-ip>:8000`, then **Add to Home Screen** (Safari: Share menu; Chrome: menu > Install app).

Notes:
- The service worker caches only the app shell (page, scripts and Leaflet). Listings and other API data are always fetched live; offline you get a red "OFFLINE" banner. After an update the next open shows the new version (see Using it day to day).
- Responses are gzipped, so the full listing list is about 0.3 MB on mobile data instead of 4.6 MB (at 4,000 listings).
- Some browsers only install from HTTPS or localhost; Android Chrome may offer a plain "Add to Home screen" shortcut over http, which still works.
- **Security:** with `--host 0.0.0.0` anyone on that network can open the app, read your profile and trigger Claude calls that spend your credit. Only do this on a network you trust.
- Optional guard: set `WGP_ACCESS_TOKEN=some-long-random-string` in `.env`. Open `http://<ip>:8000/?token=some-long-random-string` once on the phone; the server sets a cookie and you are in. This is a simple shared secret, not real authentication.
- Regenerate the icons with `python tools/make_icons.py`.

### Phone over HTTPS (optional)
Plain http works, but some browsers only fully install the PWA from HTTPS, and the access token travels unencrypted. Easiest fix: Tailscale.
1. Install Tailscale on the Mac and the phone, sign in to the same tailnet, and enable HTTPS certificates in the admin console (DNS).
2. Start the app on localhost as usual (no `--host 0.0.0.0`), then run `scripts/phone-https.sh` (wraps `tailscale serve --bg 8000`; `stop` and `status` also work).
3. Open the printed `https://<machine>.<tailnet>.ts.net` URL on the phone and Add to Home Screen.

Alternative: `mkcert` (local CA, LAN only, more fiddly). Run `mkcert -install` and `mkcert <lan-ip>`, start uvicorn with `--ssl-keyfile` and `--ssl-certfile`, and install mkcert's root CA on the phone (iOS also needs it enabled under Certificate Trust Settings).

`WGP_ACCESS_TOKEN` works the same over HTTPS (`https://<url>/?token=...`). Keep it set if other devices share your tailnet.

### Phone alerts (optional)
After each ingest the app can send one digest (max 5 listings) of listings first seen in the last 24 h with a fit score of at least `WGP_NOTIFY_MIN_FIT` (default 75). It skips dealbreakers, low-trust ads, listings you already marked, and whatever "Hide priority/premium" and "Hide befristet" hide. In group mode, flats are checked against the group. Alerts go only to you, never to landlords. Nothing is sent unless you set up a channel. During quiet hours (`WGP_NOTIFY_QUIET`, default 23-07) the digest waits.

ntfy (easiest):
1. Install the ntfy app on your phone.
2. Subscribe to a long random topic name (it acts as a password, e.g. `wgp-k3j9x2q8m4v7`).
3. Put it in `.env`: `WGP_NTFY_TOPIC=wgp-k3j9x2q8m4v7`
4. Restart the server, then test: `curl -X POST localhost:8000/notify/test` (or ⋯ menu > Phone alerts).

Telegram: create a bot with @BotFather, get your chat id from @userinfobot, set `WGP_TELEGRAM_TOKEN` and `WGP_TELEGRAM_CHAT_ID`.

A listing hidden by one of the two switches is not marked as alerted, so it can still come in a later digest (within its 24 h) if you untick the switch.

Endpoints: `GET /notify/status` (channels, threshold, quiet hours, alert filters, last error), `PUT /notify/filters` (e.g. `{"hide_promo": true, "hide_fixed_term": false}`; the UI calls it) and `POST /notify/test`.

## Database backups
On startup (if today's is missing) and then hourly, the app copies `wgpendler.db` to `backups/wgpendler-YYYY-MM-DD.db` using SQLite's backup API, which is safe while the app runs. The newest `WGP_BACKUP_KEEP` (default 7) are kept. `GET /backup/status` shows the state, `POST /backup/now` forces one.

Restore: stop the app, run `cp backups/wgpendler-YYYY-MM-DD.db wgpendler.db`, delete `wgpendler.db-wal` and `wgpendler.db-shm` if present, and start the app again.

## Environment variables

All optional. Set them in `.env` and restart.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Enables Extract, the Top 5 analysis, drafts and ad summaries |
| `WGP_AUTO_EXTRACT` | `1` | `0` stops automatic extraction after ingests |
| `WGP_AUTO_EXTRACT_LIMIT` | `25` | Max listings auto-extracted per ingest |
| `WGP_EXTRACT_MAX_PER_RUN` | `50` | Batch size of the Extract button |
| `WGP_EXTRACT_MAX_TRANSIT_MIN` | `50` | Skip extracting listings with a longer transit commute |
| `WGP_EXTRACT_MODEL` | `claude-haiku-5-5` | Model for extraction and ad summaries |
| `WGP_BEST_MODEL` | same as extract model | Model for Top 5 |
| `WGP_DRAFT_MODEL` | `claude-sonnet-5-5` | Model for drafts |
| `WGP_FOLLOWUP_DAYS` | `3` | Days a listing stays Sent with no reply before "Follow up?" |
| `WGP_INGEST_INTERVAL_MIN` | `45` | Minutes between background ingests; `0` = off |
| `WGP_FULL_REFRESH_HOURS` | `24` | Flatfox/Homegate re-fetch every ad's details at most this often; runs in between only fetch new ads (Homegate also re-priced ones). `0` = every run is a full refresh |
| `WGP_COMMUTE_BACKGROUND` | `1` | `0` stops background timetable lookups after ingests (listings keep estimates until you run `python -m backend.commute`) |
| `WGP_LEARNING_BACKGROUND` | `1` | `0` retrains the reply model inside the status/sent request instead of in the background (tests set this) |
| `WGP_LEARN_MIN_OUTCOMES` | `20` | Outcomes needed before the reply score learns from them |
| `WGP_ENABLE_RONORP` | `0` | `1` adds Ronorp back to the default sources |
| `WGPENDLER_INCLUDE_APARTMENTS` | `1` | `0` stops Flatfox/Homegate from collecting whole flats (group mode needs them) |
| `WGPENDLER_GEOCACHE` | `geocache.sqlite` in repo root | Address-to-coordinates cache |
| `WGP_BACKUP_KEEP` | `7` | Daily DB backups to keep |
| `WGP_BACKUP_DIR` | `backups/` in repo root | Backup folder |
| `WGP_ACCESS_TOKEN` | none (off) | Shared secret for phone/LAN access |
| `WGP_NTFY_TOPIC` | none | ntfy topic for phone alerts |
| `WGP_NTFY_SERVER` | `https://ntfy.sh` | ntfy server |
| `WGP_TELEGRAM_TOKEN`, `WGP_TELEGRAM_CHAT_ID` | none | Telegram alerts (both needed) |
| `WGP_NOTIFY_MIN_FIT` | `75` | Minimum fit score to alert on |
| `WGP_NOTIFY_QUIET` | `23-07` | Quiet hours (local time); set it empty to disable |
| `RUN_E2E`, `WGP_LIVE_API`, `E2E_INGEST_WAIT` | see Tests | End-to-end smoke test only |
| `RUN_UI_SMOKE`, `WGP_LEAFLET_DIR`, `WGP_CHROMIUM` | see Tests | Browser smoke test only |

## Sources

| Source | Status |
|---|---|
| Flatfox | Used (public API, tiled to get past its result cap) |
| Homegate | Used (up to 40 result pages per run) |
| WOKO | Used (student housing cooperative; few listings) |
| Ronorp | Off by default: as of 2026-10-18 its Zürich WG-room category is empty, so there is nothing to fetch. Set `WGP_ENABLE_RONORP=1` to try again later. First results page only (robots.txt disallows paging) |
| WGZimmer | Not usable: search is behind reCAPTCHA, which we don't bypass. The adapter returns nothing |
| students.ch and others | Not implemented: no permitted automated access. The app only reads what robots.txt and the site allow, at about 1 request per second |

Review of other candidate sources (ETH/UZH portal, students.ch, JUWO, Comparis, …): [docs/sources-review.md](docs/sources-review.md).

### Incremental ingest
Flatfox and Homegate remember which ads' details were already fetched (tables `source_known`, `source_state`). Each run fetches the cheap list (Flatfox map pins, Homegate result pages), but only fetches and geocodes new ads. Homegate also re-reads ads whose price changed; Flatfox does not, because its pin prices vary per session. A full refresh happens at most every `WGP_FULL_REFRESH_HOURS`; that is when other edits (including Flatfox price changes and new photos) show up.

Homegate's results carry coordinates, so no geocoding is needed. Other addresses go through the cache, then swisstopo (4 parallel workers), then Nominatim (at most 1 request per second).

One source failing never stops the others. `GET /ingest/status` shows progress and the last 5 runs. To run an ingest by hand:

```bash
curl -X POST "http://localhost:8000/ingest?sources=flatfox,homegate"
```

### Listing dates
`listed_at` (UTC `YYYY-MM-DD HH:MM:SS`) drives recency:

| Source | Date | Field |
|---|---|---|
| Flatfox | Publication time | `published` (fallback `created`) |
| Homegate | Ad creation time (an old ad that is still live looks old) | `meta.createdAt` in the page's embedded JSON |
| WOKO | None; falls back to first-seen | none |

From the ad text the app also reads the end of a befristet rental (`term_end`, only dates with a year) and a stated move-in date (`move_in_stated`), see Scoring. Both are parsed in `backend/term_dates.py`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is offline: saved fixtures, no network, no Anthropic calls.

**End-to-end smoke test** (opt-in): starts a real server, ingests, and checks the API and page. `E2E_INGEST_WAIT` (default 240 s) is how long it waits for the first ingest.

```bash
RUN_E2E=1 python tests/smoke_e2e.py                  # no Anthropic calls
RUN_E2E=1 WGP_LIVE_API=1 python tests/smoke_e2e.py   # also exercises Claude (costs credit)
```

**UI smoke test** (opt-in): starts `tools/dev_server.py` and drives headless Chromium at 1440x900 and 390x844. No network requests, no Anthropic calls. It covers the list and count, walkthrough, trust chips, detail and draft panel (solo and group, with toggles), keyboard and accessibility checks, photos, draw area, Top 5 filters, the sync line, the stats page and service-worker updates. It fails on any console error and exits non-zero on a failure.

```bash
RUN_UI_SMOKE=1 python tests/ui_smoke.py            # screenshots go to <system temp>/wgp-ui-smoke/<time>/
RUN_UI_SMOKE=1 python tests/ui_smoke.py --out DIR  # or to DIR (outside the repo)
```

It needs Playwright and Chromium (`playwright install chromium`; `WGP_CHROMIUM` points it at a specific binary). If unpkg.com is unreachable it serves Leaflet from `WGP_LEAFLET_DIR` (default `~/.cache/wgpendler/leaflet`). Fill that once with `cd ~/.cache/wgpendler/leaflet && npm pack leaflet@1.9.4 leaflet.markercluster@1.5.3`; do not commit the tarballs.

### Dev tools

```bash
python tools/dev_seed.py /tmp/wgp.db --outcomes             # synthetic DB: ~200 listings, scams, extractions, history
python tools/dev_server.py --fake-llm --outcomes --group    # app on such a DB at http://127.0.0.1:8765
python tools/bench.py --browser                             # timings on a real-size DB (4000 listings, 300 contacted)
```

`dev_server.py` works on a copy of `profile.json` and never touches `wgpendler.db`. It runs no ingest and puts backups in a temp folder. `--fake-llm` returns canned drafts and summaries (never a real Anthropic client); without it the Claude features report a missing key. `--clear-template` empties the message template so the walkthrough shows. `bench.py` times the main endpoints (list, detail, status) and shows the on-the-wire size; `--browser` also measures page load in Chromium.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'fastapi'`**: the venv is not active, see Running.
- **Empty list right after first start**: the first ingest takes several minutes; check `curl localhost:8000/ingest/status`.
- **Phone still shows an old version after an update**: open the app once more. An install from before the 2026-10-19 update fix needs one extra open (or a reload) once.
- **Ronorp returns nothing**: expected, see Sources. If you enabled it and the site has listings again, run `playwright install chromium`. WGZimmer always returns nothing.
- **"credit balance exhausted" / auth errors**: top up billing or fix `ANTHROPIC_API_KEY` in `.env`, then restart.
- **Address already in use**: another server is on port 8000; use `--port`.
- **Map tiles missing**: tiles come from Esri; check your connection.
- **Start over**: stop the server and delete `wgpendler.db` (listings and your Sent/Replied marks; backups stay in `backups/`) and optionally `geocache.sqlite`.

## Project structure

```
backend/   main.py (routes), db.py, ingest.py, ingestion/ (one adapter per source, geocode.py),
           scoring.py, term_dates.py, trust.py, filters.py, learning.py, extraction.py,
           drafting.py, summary.py, group.py, notify.py, commute.py, backup.py, access.py,
           frontend.py (static files, service-worker build id), llm.py, models.py
frontend/  index.html (map UI, served at "/"), stats.html (reply stats), checklist.js (draft checklist),
           geo.js (draw-area maths), manifest.json, sw.js, pwa.js, icons/
scripts/   phone-https.sh (optional Tailscale HTTPS helper)
tools/     make_icons.py, dev_seed.py + dev_server.py (synthetic DB, offline app), bench.py (performance)
tests/     offline pytest suite, opt-in smoke_e2e.py and ui_smoke.py
docs/      sources-review.md
profile.json   your applicant profile (edited in the UI)
```
