# WGPendler roadmap

Phases 1–7 from the original brief are done (see CHANGELOG.md). This is the polish plan, in priority order.
Each block is sized for one working session with a few parallel agents.

## Block A: accuracy (highest impact)
1. **Real commute times.** `backend/commute.py` estimates commute times from straight-line distance at 22 km/h, which ignores tram lines, transfers and the Hönggerberg hill. Use real timetable connections (transport.opendata.ch, free, rate-limited), with a weekday 8:00 arrival and caching per address. Keep the estimate as a fallback, and show which one a listing uses.
2. ~~**Incremental ingest.**~~ Done (see CHANGELOG). Flatfox fetches details for every pin, which takes about 6 min per run. Only fetch new pins and pins whose price changed, so the periodic refresh takes seconds and alerts arrive sooner.
3. ~~**Reliable listing dates.**~~ Done (see CHANGELOG). Many listings lack `listed_at`, so recency falls back to first-seen time. Parse the posting date from every source, so "fresh" really means fresh.
4. ~~**Trust score (scam check).**~~ Done (see CHANGELOG). Today scams are only partly caught, by the extracted `red_flags` and implausible-price checks. Add a 0–100 trust score with a reason for each signal:
   - price far below the area median for its size
   - payment or deposit before viewing, or "I'm abroad, keys by post"
   - contact only via WhatsApp or an external email
   - the same text or photos posted under different addresses (a classic scam pattern)
   - no street address, or a brand-new poster
   Show a shield chip on each card, hide very low trust by default, warn in the draft panel, and never alert on low-trust ads.
5. ~~**Homegate speed.**~~ Done (see CHANGELOG). Geocoding takes about 1 s per ad. Look addresses up in parallel through swisstopo (no 1 req/s limit) and skip ads already in the cache.

## Block B: reply rate
6. ~~**Learn from outcomes (Phase 6b).**~~ Done (see CHANGELOG). Once there are about 20 sent/replied marks, fit the scoring weights to what actually got replies, and show the reply rate by source, price band and message length on a small stats page.
7. ~~**Draft variants.**~~ Done (see CHANGELOG). Offer 2 tones (short and warm). Show a German/English toggle when the ad's language differs from your strongest language.
8. ~~**Ad summary.**~~ Done (see CHANGELOG). A one-line English summary for German ads in the detail view, using cached Haiku.
9. ~~**Smarter checklist.**~~ Done (see CHANGELOG). Fewer false ambers for long hard requirements, and use extracted fields instead of keyword matching.

## Block C: UI polish
10. **Typography.** Consolidate the many font sizes (.56–.8rem) into 4 steps, and keep the departure-board look.
11. **Focus and keyboard details.** Skip Leaflet controls in the tab order, keep Esc in the draft textarea from closing the detail view, and remove the pin of a listing hidden by filters after closing it.
12. **Ingest progress.** A per-source progress bar in the sync indicator. `/group/listings` should return a total ("595 of 3,325").
13. **Onboarding.** A short first-run walkthrough: profile, then group (optional), then alerts (optional).
14. **Map.** Commute-time isochrone shading around the selected campus, and a "draw area" filter.

## Block D: reach and robustness
15. ~~**Phone access over HTTPS**~~ Done (see CHANGELOG). (e.g. `tailscale serve`), so the PWA installs fully and the access token isn't sent in plain text.
16. **New sources.** Review the ToS and robots.txt of ETH/UZH housing offers and other student boards. Only add sources that permit automated access; never bypass a captcha.
17. ~~**Database backups.**~~ Done (see CHANGELOG). A daily copy of `wgpendler.db` (keep 7), because statuses and sent messages are personal history.
18. ~~**Group mode extras.**~~ Done (see CHANGELOG). Follow-up drafts for flats, and members' own budgets (not just an even split).

## Guardrails (unchanged)
Draft-and-review only, never auto-send. No fabricated facts. Respect robots.txt, ToS and anti-bot measures. Secrets only in `.env`.
