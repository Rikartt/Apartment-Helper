# Changelog

## Real commute times

### New
- Transit commute minutes now come from real timetable connections (transport.opendata.ch, 08:00 weekday arrival, including the walk to the first stop), cached per ~100 m grid cell and campus in SQLite and fetched in a background step after ingest. Straight-line estimate stays as fallback; `commute_source` (`timetable` | `estimate`) is stored per listing and the UI prefixes estimates with `~`.
- `python -m backend.commute` backfills existing listings.
- Works with the incremental ingest: listings that were only confirmed present (not re-fetched) are upgraded from the cache after each run, and the background refresh applies cache hits before making any API call.
- Map legend and card tooltips say which numbers are estimates (rings stay a straight-line estimate; bike minutes are always estimated). The `~` also marks estimates in the "N min to ETH" chip, the Message-today strip, the group "worst commute" and phone alerts.
- Bike estimate adds a climb penalty (up to 5 min) for Hönggerberg.

## Block C: UI polish

### Improved
- Typography: font sizes consolidated into four steps (`--fs-xs/sm/md/lg`), departure-board look kept.
- Keyboard and focus: Leaflet's zoom and attribution links are skipped in the tab order; Esc in the draft box only leaves the box (a second Esc closes the detail view); closing a detail removes the pin of a listing your filters now hide.
- Sync indicator: "syncing 2/5 · homegate" with a thin progress bar; on phones it temporarily replaces the listing count. The header count reads "84 of 181". `/group/listings` returns `total`, `/ingest/status` returns `current`.
- First-run walkthrough (profile, then group and phone alerts, both optional) replaces the old hints; shown only while the profile has no message template, and never again once skipped or finished.
- Map: dashed ~10/20/30 min rings around the selected campus for the chosen mode (straight-line estimate, like the commute fallback), with a legend row.

## Block D: backups, group extras, phone HTTPS

### New
- Daily database backups (`backend/backup.py`, SQLite backup API, safe while running): `backups/wgpendler-YYYY-MM-DD.db`, newest `WGP_BACKUP_KEEP` (7) kept, `WGP_BACKUP_DIR` to move them; on startup if today's is missing, then checked hourly. `GET /backup/status`, `POST /backup/now`, restore steps in the README.
- Group mode: members' own budgets (a CHF field per member in the group editor); a flat is a dealbreaker when a member would pay more than 15% over their budget, naming who. Rent splits by room size when every member has `room_m2` and the ad gives the living area.
- Group follow-up drafts for flats (`POST /group/draft/{id}?followup=1`, and the "Draft follow-up" button in group mode). Like solo follow-ups they reuse no profile facts and never repeat the first message.
- Phone over HTTPS: `scripts/phone-https.sh` (wraps `tailscale serve`), with a mkcert alternative in the README.

### Fixed
- The test suite no longer writes a backup of its test database into the repo's `backups/` folder.

## Learn from outcomes (Phase 6b)

### New
- Reply stats page (`/stats.html`, menu ⋯ > Reply stats): reply rate by source, message length, rent, how soon you wrote, fit and commute, plus median reply time. Inline SVG bars, no chart library.
- The reply score learns from your outcomes once there are `WGP_LEARN_MIN_OUTCOMES` (default 20): a small logistic model (pure Python, weights in `learned_weights` / `learned_model`) is blended in, growing with the number of outcomes up to 70%. `GET /stats` gains the outcome keys (existing keys unchanged).
- Training runs only when outcomes change (status or sent-message endpoints) and on the stats page; scoring reads a cached model, so listing requests never retrain.

## Block B: drafts, summary, checklist

### New
- Draft variants: `POST /draft/{id}?tone=short|warm&lang=auto|de|en`; tone toggle and (when the ad language differs from your strongest profile language) a DE/EN toggle in the draft panel. Changing an option regenerates, asking first if you edited the text.
- Ad summary: `GET /listings/{id}/summary` (cached Haiku, table `ad_summaries`), shown under the title for non-English ads; hidden on any error.
- Checklist: flat conditions ("no pets", "noise-sensitive people unsuitable") are no longer items; extracted move_in/duration tick date/duration items; long requirements match on a third of the key words; "für längere Zeit" / "for a long time" count as a stay duration; long items no longer break onto separate lines.

## Block A: ingest (incremental, dates, geocoding)

- Incremental Flatfox and Homegate ingest (new/price-changed ads only, daily full refresh, `WGP_FULL_REFRESH_HOURS`); gone detection unchanged.
- `listed_at` normalised to UTC; Homegate now gets the ad creation date and coordinates from its embedded JSON. WOKO has no per-object date.
- Geocoding: `geocode_many` (cache first, 4 parallel swisstopo workers, Nominatim still <=1 req/s); tests can no longer reach the network geocoders.

## Trust score (scam check)

### New
- 0-100 `trust_score`, `trust_level` (high / medium / low) and `trust_reasons` on `/listings`, `/listings/{id}` and `/group/listings`, computed in `backend/trust.py` (no LLM): price far below the local median, scam phrases (DE/EN/FR/IT), duplicate descriptions at other addresses or prices, missing house number / photos, agency poster, extracted red flags.
- Low-trust listings are hidden by default ("Show low-trust" in More filters), show a warning in the detail view above the draft button, and are left out of phone alerts and the Message-today strip. Shield chip on cards and in the detail view.
- Fast enough for the full database: neighbours for the price median come from a per-kind grid, and scores are computed once after each ingest and cached until the listings change.

## Follow-up loop

### New
- Draft checklist (what the ad asked for, ticked heuristically), sent-message history (`sent_messages` table, `POST /listings/{id}/sent`), "Follow up?" chip and Follow-ups filter (`WGP_FOLLOWUP_DAYS`, default 3), optional follow-up draft (`POST /draft/{id}?followup=1`), and a Copy & open ad button.

## 2026-10-18 overnight

### New
- **Phone / PWA**: installable (manifest, icons, service worker for the app shell, never caches listings, offline banner), phone access docs, optional `WGP_ACCESS_TOKEN` guard.
- **Group mode**: a Solo | Group toggle. Group mode finds whole flats, scores them per person (worst-case commute across campuses), and drafts a first message from the group's members and notes.
- **Fresh listings**: background re-ingest every 45 minutes, NEW badges, a "New only" toggle, a live notice when new listings arrive, and gone-listing detection (hidden, your Sent/Replied marks kept).
- **Phone alerts** (opt-in): ntfy or Telegram digest of new high-fit listings, with a fit threshold and quiet hours. `GET /notify/status`, `POST /notify/test`.
- **More sources and better yield**: WOKO added, deeper Homegate paging, Flatfox tiling to get past its 1000-result cap, cross-source duplicate removal, shared address geocoder with a persistent cache.
- **UI overhaul**: clearer listing cards with fit chips and dealbreakers, commute to both campuses, sized price pins with clustering, Message-today strip, "More filters" drawer, listing type filter (WG rooms / studios), keyboard shortcuts (press `?`), better mobile layout, higher-contrast text.
- Ingest progress and history in `/ingest/status`; Sent / Replied / Skipped tracking with stats.

### Improved
- Ronorp is off by default: its Zürich WG-room category is empty on the site. The adapter now detects that and returns quickly (`WGP_ENABLE_RONORP=1` to re-enable).
- Fit scores recalibrated so good listings no longer all saturate at 100; implausible prices are flagged as dealbreakers.
- Drafts: a move-in date in the past reads as "available immediately"; group drafts never invent documents.
- Extraction is bounded (prefilters, per-run cap), failures show clear messages (for example exhausted credit), and concurrent extractions are single-flight.
- Basemap switched to Esri (CARTO now requires an API key).
- Startup no longer blocks on the first ingest.

### Fixed
- Database connections are closed properly; profile saves are atomic; a malformed profile no longer crashes the app.
- Top 5 no longer suggests whole flats.
- Gone detection ignores truncated or unhealthy runs, so listings are not wrongly hidden.
- Homegate no longer invents a location when none is given.

### Tests
- Offline pytest suite (scoring, DB, adapters on saved fixtures, API, freshness, group) and an opt-in end-to-end smoke script.
