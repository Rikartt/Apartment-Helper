# Changelog

## Learn from outcomes (Phase 6b)

### New
- Reply stats page (`/stats.html`, menu ⋯ > Reply stats): reply rate by source, message length, rent, how soon you wrote, fit and commute, plus median reply time. Inline SVG bars, no chart library.
- The reply score learns from your outcomes once there are `WGP_LEARN_MIN_OUTCOMES` (default 20): a small logistic model (pure Python, weights in `learned_weights` / `learned_model`) is blended in, growing with the number of outcomes up to 70%. `GET /stats` gains the outcome keys (existing keys unchanged).
- Training runs only when outcomes change (status or sent-message endpoints) and on the stats page; scoring reads a cached model, so listing requests never retrain.

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
