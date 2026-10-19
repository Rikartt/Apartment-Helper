# Changelog

## 2026-10-19: night review 2 (term dates)

### Fixed
- "mid-November 2026" / "early-December 2026" was read as the END of a befristet rental (the hyphen counted as a range dash), so such ads became "Rental period already over" once that month passed, e.g. a live Wipkingen ad whose lease runs to October 2027. 4 of 4,213 live ads were affected; ends written "until end-March 2027" still work.
- A yearless move-in date just before New Year ("frei ab 20. Dezember" in an ad published 5 January) was read as next December, so the ad got "Available too late". It now counts as last December (same 31-day slack as within a year).

### Notes
- Stored `term_end` / `move_in_stated` values correct themselves on the next upsert of the ad (the regular full refresh, default within 24 h).
- New `tests/test_night_review2.py` also round-trips every column through `upsert_listing` (insert and update): the hand-merged column list, placeholders and values line up.

## 2026-10-19: Flatfox placeholder streets

### Fixed
- Flatfox street fields that are placeholders ("----", "-", "W", the postcode "8426", or the town again as in "Dietikon, 8953 Dietikon") now count as no street, so the address is "8053 Zürich" instead of "----, 8053 Zürich". "8426, 8426 Lufingen" no longer passes for a house number in the cross-source duplicate key or trust's address checks. Live DB (4,213 ads): 10 addresses change, 1 dedupe key goes away (no duplicate group changes), no trust score changes. A short street that could be real ("Zür") is kept.

### Notes
- Street-less Flatfox ads cannot get their street back from the URL slug: when a poster hides the street, `street` is null and Flatfox builds `public_address` and the slug from postcode + place only ("8057-zurich"). 0 of 1,090 street-less ads in the live DB had a street in the slug; a live re-check of 50 showed the same.

## 2026-10-19: photos in the detail view

### Added
- The detail view shows the listing's photos: a sideways-scrolling strip of lazy-loaded thumbnails (`loading="lazy"`, `decoding="async"`, `referrerpolicy="no-referrer"`, alt "Photo 2 of 7") and a full-size viewer (‹ › buttons, arrow keys, swipe; Esc or ✕ closes it and returns focus to the thumbnail; focus trap; 40px targets on phones). List cards stay without images.
- Image URLs are stored in a new `photos` column (JSON, at most 12) filled at ingest: Flatfox detail requests now ask for `expand=images` (same request, the image objects instead of bare IDs; thumbnail `url_thumb_m` + full `url`), Homegate takes them from the embedded result JSON (`localization.<lang>.attachments`, type IMAGE). `photo_count` (a trust input) is unchanged and still counts every photo.
- Images load directly from the source CDN; the app neither proxies nor caches them (the service worker leaves cross-origin requests alone).

### Notes
- No backfill is possible: no stored data holds image URLs (the DB keeps the ad text, not the source JSON), so existing rows get photos on the next regular Flatfox full refresh (default within 24 h). Adding the column deliberately does not force a full refresh.
- `photos` is read only by `GET /listings/{id}`: `/listings`, `/group/listings`, trust, notify and Top 5 never select it (`db.DETAIL_ONLY`), so the big list payload and its timing are unchanged. Storage: about 2 KB per Flatfox listing, roughly 8-9 MB at 4,200 listings.

## 2026-10-19: implausible move-in dates, Flatfox postcodes

### Fixed
- An ad whose structured move-in date lies more than 60 days before its own publication (Flatfox live: 2004-11-01, 1972-11-01, or last year's date from the date picker) now scores on the move-in date its text states, if it states one: "ab 1. November", "from 17 November", "Per 01.11.2026", "verfügbar ab Mitte November", "dès le 1er novembre", "per sofort" / "available immediately" (= the publication date). A date without a year is taken as its next occurrence after publication (up to a month before publication still counts as this year). Before, such an ad counted as "available now" and got "Move-in fits" (live #1816: field 2004-11-01, text "available from 17 November"). Without a date in the text, an old date still means "available now". `available_from` is never overwritten: the text's date goes into a new `move_in_stated` column at ingest (existing rows are filled from their stored text on startup, no refetch), the parser lives next to `term_end`'s in `backend/term_dates.py`. Cards, the detail and Top 5 show that date with a `*`/note naming the ad's date field; drafts and the Top 5 analysis get it too. On the 4,213-listing live DB: 22 ads have such a date, 18 state a date in the text, 5 change "Move-in fits", 1 fit score changes (53 to 44), Top 5 unchanged.
- Flatfox ads without a street stored only the city ("Zürich"), losing the postcode. The address is now "Street 12, 8004 Zürich" or "8004 Zürich" (postcode from `zipcode`, else the slug, which has it on 4212 of 4213 live ads), and a "Hohlstrasse, 36" street keeps its number. The cross-source duplicate check and trust's same-address check ignore a bare "8004 Zürich" exactly like they ignored "Zürich", so street-less ads in one postcode are not merged. Trust's price comparison now finds these ads by postcode too (live simulation: 40 trust scores move, 3 from medium to high). Existing rows get the new address at their next refresh.

## 2026-10-19: instant Sent / Replied / Skipped buttons

### Changed
- `POST /listings/{id}/status` and `POST /listings/{id}/sent` no longer wait for the learned reply model to retrain. They queue `learning.refresh()` on one background thread (`learning.schedule_refresh()`) and answer straight away: at 4000 listings with 300 contacted, a status tap goes from about 305 ms to 11-15 ms (`tools/bench.py`; the retrain itself still takes about 300 ms, now after the reply). The retrain starts once no tap came for 1.5 s (`learning.QUIET_S`), so a burst of taps is not slowed by a CPU-bound fit in between (on a live dev server, six taps 0.4 s apart: 7-28 ms each; without the quiet period, taps during a fit took 120-160 ms). Taps coalesce: per database at most one retrain waits behind the running one, and it starts after the last tap, so it sees every change. Until it is stored, scores use the previous model, as before a retrain. Training still happens only in `refresh()` and on the stats page (`sync()`), which waits for a retrain in flight, so `/stats` is always current. All fits are serialised by a lock, and the worker pins the database file it was asked to train. `WGP_LEARNING_BACKGROUND=0` keeps the old inline retrain (tests run that way).
- `tools/bench.py` waits for background retrains between timed calls and adds a "status + background retrain done" row.

## 2026-10-19: befristet ads whose rental period is over

### Fixed
- A befristet listing whose rental period has already ended is now a dealbreaker, "Rental period already over (ended YYYY-MM-DD)", in solo and group mode, so it never reaches Top 5 or phone alerts. Flatfox has no end-of-rental field (only `moving_date` and `is_temporary`), so the end is read from the ad's own words ("bis zum 30.09.2026", "until January 10, 2027", "Dec 18 – Jan 4, 2026"; only dates with a year, latest one wins) by the new `backend/term_dates.py` and stored in a new `term_end` column at ingest (existing rows are filled from their stored text on startup, no refetch). It stays conservative: an ad whose start is still ahead, or whose end lies before its own publication date (a wrong-year typo), is never "over".
- A befristet ad whose stated start is more than 60 days old no longer gets "Move-in fits". Live, these were almost all fresh ads with the wrong year in Flatfox's date picker (e.g. moving date 2025-12-18 on an ad published 2026-10-19 for Dec 2026 – Jan 2027), which ranked #1 in Top 5 on a move-in date that is really two months away. Old start dates on open-ended ads still mean "available now".

## 2026-10-19: smaller /listings over the wire, instant single-listing replies

### Changed
- Responses are gzipped when the browser accepts it (Starlette `GZipMiddleware`, level 6, replies under 1 kB and PNG icons left alone). At 4000 listings `/listings?kind=wg_room,studio` goes from 4.6 MB to about 0.3 MB on the wire, which is what matters on mobile data; the server spends roughly 40-90 ms more per call compressing. The service worker is unaffected (the browser decodes before it sees the body).
- `/listings` and `/group/listings` leave out columns no page reads: `extract_attempts`, `extracted_at`, `missed_runs`, `notified_at`, `last_seen_at` (`db.LIST_OMIT`), and `/listings` also drops `fit_reasons`, a copy of `match_reasons` (the UI already reads either). About 15% less JSON. `GET /listings/{id}` and `/listings?include_raw=1` still return everything.
- `GET /listings/{id}` and the status/sent POSTs no longer re-read and hash all listings to look up one trust score: a `trust_gen` token in the DB changes (SQLite triggers) whenever a column the trust score reads is inserted, changed or deleted, and while it matches the cached annotation the cached result is used. Detail view: about 110 ms to 9 ms at 4000 listings, with identical results (tests/test_payload.py).
- `tools/bench.py` also times the detail and status endpoints and shows the on-the-wire size.

## 2026-10-19: accessibility and keyboard pass

### Fixed
- Keyboard: Esc now returns focus to whatever opened the thing it closes (⋯ menu, Keyboard shortcuts, Top 5, Profile/Group panels; closing the detail puts focus back on the card). Tab stays inside an open modal or side panel. Space opens a focused card like Enter, and focus moves into the detail ("← Listings") instead of getting lost when the list hides. ↑/↓/Home/End move through the ⋯ menu, and tabbing out of it closes it.
- Contrast (WCAG AA on the dark theme): red text (rents, headings) uses a new `--red-txt` (#f44336); `#c00` stays for borders and buttons. Active Transit/Bike buttons use darker blue/green under the white text. The FIT/REPLY labels inside badges are no longer faded. Commute times, "CHF" after rents, greys in the detail/Top 5/popup, map ring labels, popup links and the stats page's outcome counts were nudged until they pass.
- Screen readers: the ↗ link on cards, the Tone/Language toggles and the map now have names; the ETH campus pins are no longer unnamed tab stops; the page has header/main/filters landmarks. The listing count is announced once when filtering settles (a debounced hidden live region), not on every slider step.
- At phone width the main actions (top bar, filter and sort toggles, detail back, Draft, draft actions, panel buttons) are at least 40px tall; the card's Sent/Replied/Skipped/draft/↗ buttons keep their look but get an invisible 40px hit area.
- `tests/ui_smoke.py` checks all of the above at 1440 and 390 (`run_a11y`).

## 2026-10-19: fixes from a live Flatfox end-to-end run

### Fixed
- Flatfox listings now use the ad's own headline as their title (`description_title`, whitespace collapsed, max 200 chars). Before, every room was titled "1.0-Zimmer WG-Zimmer in Zürich – N CHF" (shown as "WG-Zimmer in Zürich"), so the list, Message today and Top 5 showed five identical names. The generated title stays as the fallback for ads without a headline. Existing rows pick up the new title at the next full refresh (`WGP_FULL_REFRESH_HOURS`, default 24).
- Trust scoring was slow on real data: with ~4200 real Flatfox ads (6.4 MB of text) a cold `/listings` took about 13-20 s, nearly all of it the scam-phrase regexes. Each phrase rule now has a keyword gate (`_TEXT_KEYS` in `trust.py`): its regex only runs when one of the words any match must contain is in the ad. Same results on all 4213 live ads, regex time 9.9 s to 1.8 s, a cold trust pass 20 s to 4.5 s. The gate sits inside `_text_signals`, under the per-text memo cache; cache warming is the one mechanism already on main (startup skip, after extraction, after ingest), not doubled.
- Group cards and the detail view showed minus reasons ("CHF 1350 pp over budget", "room count unknown", "2 red flags") as green ✓ chips. They are now amber "!" chips.

## 2026-10-19: Top 5 follows the priority, befristet and area filters

### Fixed
- Top 5 ignored the list filters, so with "Hide priority/premium", "Hide befristet" or a drawn area it could still suggest exactly those listings. `GET /best` now takes `hide_promo`, `hide_fixed_term` and `area` (`lat,lng;lat,lng;...`, as drawn) and filters before it picks the five, so you still get five picks; the UI passes the current state and names the active filters at the top of the Top 5 window. The rent slider, Show tab and New only deliberately still don't apply (Top 5 ranks against your profile, not the current view). Top 5 stays solo-only.
- The Top 5 analysis cache keeps the last 8 sets of picks instead of one, so ticking a filter and unticking it again does not pay for a second Claude call.

### Changed
- `promo_active()` and `is_fixed_term()` moved from `notify.py` to a new `backend/filters.py` (with the server copy of the point-in-polygon test), shared by phone alerts and Top 5.

## 2026-10-19: stats page stops re-attempting a hopeless training run

### Fixed
- While every resolved outcome is the same class (e.g. only "no reply" so far), no model can be trained, so nothing was cached and every `GET /stats` re-ran the training attempt: a second scan of all contacted listings plus delete-writes to the `learned_weights`/`learned_model` tables, on each page load. The learning module now remembers the outcome signature of its last attempt (resolved outcomes, replies among them, threshold), whether or not it produced a model, and `sync()` re-attempts only when that signature changes: a new outcome, a no-reply that turns into a reply, or one dropping out. Zero and too-few outcomes never train, as before. Scoring still only reads the cached model; training still happens only in `refresh()` and `sync()`.

## 2026-10-19: draw an area on the map

### New
- "✎ Draw area" map button: click or tap corners to draw a polygon (close it on the first corner, with a double-click or Done; Esc or Cancel aborts, without closing an open listing). Only listings inside it stay in the list, on the map and in Message today; it counts in "More filters (n)", shows as a dashed amber outline, is kept in localStorage, and "✕ Clear area" or reset removes it. Pins ignore taps while drawing, so corners can go on top of them. Plain Leaflet, no new library; the point-in-polygon test is in `frontend/geo.js` (node unit tests in `tests/test_geo.py`, browser checks at both sizes in `tests/ui_smoke.py`).

## 2026-10-19: follow-ups in the language of your first message

### Changed
- Follow-up drafts (`?followup=1` on `/draft/{id}` and `/group/draft/{id}`) are now written in the language of the first message you actually sent, not the ad's language. Example: a German ad you answered in English (lang=en) gets an English follow-up. The language is read from the stored first message with the same German/English word heuristic the app already uses for the draft's language tag (`text_language` / `followup_language` in `drafting.py`, shared by solo and group). If it can't be told (very short text), the follow-up stays in the ad's language with exactly the old prompt; with no stored first message there is still nothing to follow up on (409). The response's `language` field follows the same rule. Draft-and-review only, nothing is sent.
- The follow-up draft's language note compares with your first message ("matches your first message") instead of the ad.

## 2026-10-19: phone alerts follow the priority and befristet filters

### New
- Phone alerts now skip what "Hide priority/premium" and "Hide befristet" hide on the map. Before, the switches were saved only in the browser, so the digest after an ingest could still announce a Flatfox Priority or befristet listing you had hidden. The app now also saves them on the server (`PUT /notify/filters`, stored in the database, shown in `GET /notify/status`); a browser with its own saved choice pushes it on load, a browser without one takes the server's. Priority is skipped only while its period runs (no end date, or an end in the future), as on the map. Befristet uses the same rule as the map (Flatfox flag, else ad wording, else Extract's duration). If your profile wants a long-term room, befristet listings were already left out as dealbreakers; the switch now also covers profiles without that wish and Extract-only sublet/fixed-term durations. Hidden listings are not marked as alerted, so unticking a switch can still bring them up in a later digest within the 24 h window. Group-mode apartments follow the switches too.

## 2026-10-19: tone and language for group drafts

### New
- Group drafts get the same Warm/Short and language toggles as solo drafts. `POST /group/draft/{id}` takes `?tone=warm|short&lang=auto|de|en` (anything else: 422, as on `/draft/{id}`). Short is 3-4 sentences and still makes the honest flat-share point; every group rule stays (only facts from the group profile, nothing invented about members, "available immediately" handling). Defaults produce exactly the old prompt; follow-ups (`?followup=1`) ignore both options.
- The language toggle also shows for listings without Extract data (all flats), using the same text guess as the draft's language tag.

### Changed
- Solo and group drafts share one tone/language helper in `drafting.py` (`language_choice`, `short_rule`, `normalize_tone`, `MOVE_IN_RULE`); the solo prompts are unchanged, word for word.

## 2026-10-19: befristet counts as a dealbreaker

### New
- If your profile wants a long-term/permanent room (and does not accept sublets), a listing flagged befristet (`fixed_term`: Flatfox's `is_temporary`, else ad wording) now gets the "Duration: fixed term" dealbreaker even before Extract, and the "Long-term" chip is not shown for it. A source saying "not befristet" never clears an extracted fixed-term/sublet duration; listings with both give one reason. Scores shown in the app are computed live, so this applies on the next page load without re-ingesting. Group mode has no duration check and is unchanged.

## 2026-10-19: hide befristet (fixed-term) lets

### New
- "Hide befristet" in More filters and a BEFRISTET chip. Uses Flatfox's own `is_temporary` flag (live: 17 of 50 sampled Flatfox ads; the flag matched the titles exactly), else ad wording (befristet but not unbefristet, Zwischenmiete, temporär, sublet, "für 6 Monate"; agrees with Flatfox's flag on all 50 sampled ads), else the duration from Extract. New `fixed_term` column; the first ingest after updating re-fetches details once to fill it.

## 2026-10-19: priority/premium filter, steadier Message today

### New
- Paid placements are detected during ingest (new `promo` / `promo_until` columns), shown with a PRIORITY/PREMIUM chip, and hidden with one "Hide priority/premium" switch in More filters. Flatfox Priority comes from Flatfox's own field `flatfox_priority_exclusive_until`: only paying Priority members can apply until that date, after which the listing counts as normal again (live check: 611 of ~4300 Flatfox listings were in their Priority period). Other sources use a best-effort match. The first ingest after updating re-fetches Flatfox/Homegate details once, so existing listings are checked too.
- Flatfox photo counts are stored (`photo_count`), which turns on the trust score's "no photos" signal.

### Fixed
- Incremental Flatfox ingest re-fetched ~1800 ads every run: the pin API reports the same ad at different prices per session (e.g. 2100 vs 2720), so the price looked changed. Incremental runs now fetch only new pins (live: 144 instead of ~1800, 55 s instead of 139 s); price and text edits arrive with the daily full refresh.
- The Message today strip disappeared outside the "To do" view, with "New only" on, or when fewer than two picks qualified. It now uses your filters but not the Show tab or New only, and shows from one pick.

## 2026-10-19: small fixes

### Fixed
- Map: hiding a listing's pin with a filter right after closing its detail (while the map was still zooming to it) broke the map until reload; every later listing failed to open. The pin now stays until the zoom ends and is removed then. The UI smoke test checks this fast case.
- Trust score cache: an ad whose text changed but kept its length, or whose pin moved, could keep its old score.
- The Top 5 and keyboard-shortcut windows no longer share one HTML id.

## 2026-10-19: real commute times (PR #4)

### New
- Transit commute minutes now come from real timetable connections (transport.opendata.ch, 08:00 weekday arrival, including the walk to the first stop), cached per ~100 m grid cell and campus in SQLite and fetched in a background step after ingest. Straight-line estimate stays as fallback; `commute_source` (`timetable` | `estimate`) is stored per listing and the UI prefixes estimates with `~`.
- `python -m backend.commute` backfills existing listings.
- Works with the incremental ingest: listings that were only confirmed present (not re-fetched) are upgraded from the cache after each run, and the background refresh applies cache hits before making any API call.
- Map legend and card tooltips say which numbers are estimates (rings stay a straight-line estimate; bike minutes are always estimated). The `~` also marks estimates in the "N min to ETH" chip, the Message-today strip, the group "worst commute" and phone alerts.
- Areas without a usable timetable result (no stop within walking range, or no connection) are remembered for 7 days instead of being looked up again after every ingest.
- Bike estimate adds a climb penalty (up to 5 min) for Hönggerberg.

## 2026-10-19: after the merges (PRs #13, #14)

### New
- Opt-in browser smoke test (`RUN_UI_SMOKE=1 python tests/ui_smoke.py`) and dev tools: `tools/dev_seed.py` (synthetic DB) and `tools/dev_server.py` (app on that DB, optional fake LLM, no ingest).

### Fixed
- Listing age ("2 days ago", fresh-ad chip, Message-today boost) read the normalised UTC `listed_at` as local time, and Safari could not parse it at all.
- `/group/listings`: the "no WG / students" and scam text checks never saw the ad body (it was stripped before scoring); flats now carry `followup_due` / `days_since_sent` / `sent_at`, so the Follow-ups filter and "Follow up?" chip work in group mode.
- Top 5 no longer suggests low-trust (likely scam) listings.

## 2026-10-19: polish blocks A-D (PRs #5-#10)

Newest merge first.

### UI polish (Block C, #8)
- Typography: font sizes consolidated into four steps (`--fs-xs/sm/md/lg`), departure-board look kept.
- Keyboard and focus: Leaflet's zoom and attribution links are skipped in the tab order; Esc in the draft box only leaves the box (a second Esc closes the detail view); closing a detail removes the pin of a listing your filters now hide.
- Sync indicator: "syncing 2/5 · homegate" with a thin progress bar; on phones it temporarily replaces the listing count. The header count reads "84 of 181". `/group/listings` returns `total`, `/ingest/status` returns `current`.
- First-run walkthrough (profile, then group and phone alerts, both optional) replaces the old hints; shown only while the profile has no message template, and never again once skipped or finished.
- Map: dashed ~10/20/30 min rings around the selected campus for the chosen mode (straight-line estimate, like the commute fallback), with a legend row.

### Backups, group extras, phone HTTPS (Block D, #5)
- Daily database backups (`backend/backup.py`, SQLite backup API, safe while running): `backups/wgpendler-YYYY-MM-DD.db`, newest `WGP_BACKUP_KEEP` (7) kept, `WGP_BACKUP_DIR` to move them; on startup if today's is missing, then checked hourly. `GET /backup/status`, `POST /backup/now`, restore steps in the README.
- Group mode: members' own budgets (a CHF field per member in the group editor); a flat is a dealbreaker when a member would pay more than 15% over their budget, naming who. Rent splits by room size when every member has `room_m2` and the ad gives the living area.
- Group follow-up drafts for flats (`POST /group/draft/{id}?followup=1`, and the "Draft follow-up" button in group mode). Like solo follow-ups they reuse no profile facts and never repeat the first message.
- Phone over HTTPS: `scripts/phone-https.sh` (wraps `tailscale serve`), with a mkcert alternative in the README.
- Fixed: the test suite no longer writes a backup of its test database into the repo's `backups/` folder.

### Learn from outcomes (Phase 6b, #9)
- Reply stats page (`/stats.html`, menu ⋯ > Reply stats): reply rate by source, message length, rent, how soon you wrote, fit and commute, plus median reply time. Inline SVG bars, no chart library.
- The reply score learns from your outcomes once there are `WGP_LEARN_MIN_OUTCOMES` (default 20): a small logistic model (pure Python, weights in `learned_weights` / `learned_model`) is blended in, growing with the number of outcomes up to 70%. `GET /stats` gains the outcome keys (existing keys unchanged).
- Training runs only when outcomes change (status or sent-message endpoints) and on the stats page; scoring reads a cached model, so listing requests never retrain.

### Drafts, summary, checklist (Block B, #7)
- Draft variants: `POST /draft/{id}?tone=short|warm&lang=auto|de|en`; tone toggle and (when the ad language differs from your strongest profile language) a DE/EN toggle in the draft panel. Changing an option regenerates, asking first if you edited the text.
- Ad summary: `GET /listings/{id}/summary` (cached Haiku, table `ad_summaries`), shown under the title for non-English ads; hidden on any error.
- Checklist: flat conditions ("no pets", "noise-sensitive people unsuitable") are no longer items; extracted move_in/duration tick date/duration items; long requirements match on a third of the key words; "für längere Zeit" / "for a long time" count as a stay duration; long items no longer break onto separate lines.

### Ingest: incremental, dates, geocoding (Block A, #10)
- Incremental Flatfox and Homegate ingest (new/price-changed ads only, daily full refresh, `WGP_FULL_REFRESH_HOURS`); gone detection unchanged.
- `listed_at` normalised to UTC; Homegate now gets the ad creation date and coordinates from its embedded JSON. WOKO has no per-object date.
- Geocoding: `geocode_many` (cache first, 4 parallel swisstopo workers, Nominatim still <=1 req/s); tests can no longer reach the network geocoders.

### Trust score: scam check (Block A, #6)
- 0-100 `trust_score`, `trust_level` (high / medium / low) and `trust_reasons` on `/listings`, `/listings/{id}` and `/group/listings`, computed in `backend/trust.py` (no LLM): price far below the local median, scam phrases (DE/EN/FR/IT), duplicate descriptions at other addresses or prices, missing house number / photos, agency poster, extracted red flags.
- Low-trust listings are hidden by default ("Show low-trust" in More filters), show a warning in the detail view above the draft button, and are left out of phone alerts and the Message-today strip. Shield chip on cards and in the detail view.
- Fast enough for the full database: neighbours for the price median come from a per-kind grid, and scores are computed once after each ingest and cached until the listings change.

## 2026-10-18 overnight

### New
- **Follow-up loop**: draft checklist (what the ad asked for, ticked heuristically), sent-message history (`sent_messages` table, `POST /listings/{id}/sent`), "Follow up?" chip and Follow-ups filter (`WGP_FOLLOWUP_DAYS`, default 3), optional follow-up draft (`POST /draft/{id}?followup=1`), and a Copy & open ad button.
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
