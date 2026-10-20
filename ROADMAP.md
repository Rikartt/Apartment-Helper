# WGPendler roadmap

Phases 1–7 from the original brief and polish blocks A–D are done, except item 16 (see CHANGELOG.md for details). Open items are at the top.

## Open
1. **Paid-placement filters per type.** Today one "Hide priority/premium" switch hides every kind. Split it per kind (Flatfox Priority, Homegate premium/top, ...) once we know which variants show up and which are worth keeping. Detection outside Flatfox Priority is a best-effort match until each field is confirmed.
2. **Top 5 in group mode.** Top 5 is solo-only. A group version would rank whole flats by group fit and analyse them with the group's facts only.
3. **WGZimmer via email alerts.** WGZimmer's search is behind reCAPTCHA, so it is not scraped. Its own email alerts could be imported instead. Waiting on the owner's first WGZimmer alert email to see the format.
4. **New sources (item 16).** The review is done ([docs/sources-review.md](docs/sources-review.md)); no candidate clearly permits automated access yet. Next steps: read robots.txt and terms from a normal connection, ask students.ch and the UZH Alumni Marktplatz, and ask Flatfox whether low-rate personal use of its public endpoint is fine. Never bypass a captcha.

## Done on 2026-10-19 (PRs #18–#36)
- Hide priority/premium and Hide befristet filters, with chips; phone alerts follow them (`PUT /notify/filters`).
- Befristet as a duration dealbreaker; befristet ads whose rental period is over are dealbreakers too.
- Draw-area map filter.
- Top 5 honours Hide priority/premium, Hide befristet and the drawn area; the analysis cache keeps the last 8 sets.
- Group drafts get the Warm/Short and language toggles; follow-ups are written in the language of your first message.
- Learning: no retraining loop on one-class outcomes, retry after a crash, retrain in the background so status taps return at once.
- Performance on real-size data: gzip, slimmer list payload, cached trust for single listings, `tools/bench.py`.
- Live-data fixes: Flatfox headlines as titles, faster trust scoring, amber warning chips in group mode, Flatfox postcodes and placeholder streets, move-in dates read from the ad text when the date field is implausible.
- Accessibility pass: keyboard focus, contrast, screen-reader labels, 40 px tap targets on phones.
- Photos in the detail view.
- Code updates reach the installed PWA (no-cache headers, service-worker build id, "updated, Reload" notice).

## Done earlier (blocks A–D)
- A, accuracy: real commute times, incremental ingest, reliable listing dates, trust score, faster Homegate geocoding.
- B, reply rate: learning from outcomes with a stats page, draft tones and language toggle, ad summaries, smarter draft checklist.
- C, UI polish: four font sizes, focus and keyboard fixes, ingest progress bar, first-run walkthrough, commute rings on the map.
- D, reach and robustness: phone access over HTTPS (Tailscale helper), daily database backups, group-mode follow-ups and per-member budgets.

## Guardrails (unchanged)
Draft-and-review only, never auto-send. No fabricated facts. Respect robots.txt, ToS and anti-bot measures. Secrets only in `.env`.
