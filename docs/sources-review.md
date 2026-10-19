# Sources review (ROADMAP item 16)

**Date:** 2026-10-19. **Scope:** research only; no adapter added, no code changed.

## How this was checked, and what that means

The research sandbox's network policy blocked direct requests to **every** candidate host. `curl` got a 403 from the egress proxy, and the fetch tool could not resolve the hosts. As a result:

- **No robots.txt could be read directly.** The robots.txt column below only quotes what earlier work in this repo recorded (the WOKO and Ronorp adapter notes). Every other row says "not verified".
- ToS findings come from web-search results that quote the operators' own legal pages. Where the clause was quoted, it is quoted and linked. Where only the existence of a page was found, the row says so.
- The block was not worked around: no alternative proxies, mirrors or caches were used to reach the sites.

Every row is therefore **"not verified directly"** unless it says otherwise. Before anyone writes an adapter, they must read the source's robots.txt and current terms from a normal connection.

## Table

| Source | robots.txt | Terms of use (automated access) | Official API / feed | Walls | Zürich listings (rough) | Recommendation |
|---|---|---|---|---|---|---|
| **ETH/UZH Housing Portal** ([wohnen.ethz.ch](https://www.wohnen.ethz.ch/en)), the joint "Zimmer- und Wohnungsvermittlung UZH/ETH" | Not verified | Moot. Listings are members-only: "The Housing Portal is exclusively available to members of the University of Zurich and ETH Zurich" ([login page](https://www.wohnen.ethz.ch/en/housing-portal/login-for-registered-users.html)) | None found | **Login** (SWITCH edu-ID; future students upload an admission letter, activation within 48 h; [registration](https://www.wohnen.ethz.ch/en/housing-portal/registration-for-future-university-members.html)) | Unknown (not public) | **Don't add.** Automating a personal university login is exactly what the guardrails exclude. The user should use the portal by hand once activated. Ask the housing office (zimmervermittlung@ethz.ch) only if they offer an alert or feed. |
| **UZH Alumni Marktplatz** (Wohnen) | Not verified | Not found | None found | A 2017 [UZH news article](https://news.uzh.ch/en/articles/2017/marktplatz.html) says the board is open to all without login. The current state is unverified | Small (unknown) | **Ask the site** (UZH Alumni) before adding. Low volume. |
| **ETH/UZH notice boards, VSUZH, ESN** | n/a | n/a | n/a | Physical boards, or link lists ([VSUZH](https://www.vsuzh.ch/en/wohnen)) that point to the other sources here | n/a | **Don't add** (nothing machine-readable of their own). |
| **WOKO** (woko.ch), already used | Allows everything (as recorded in `backend/ingestion/woko_adapter.py`: "woko.ch/robots.txt allows everything"). Not re-verified today | No terms-of-use page found by search. The site is a student housing cooperative's own vacancy list | None | None | ~10–30 free rooms at a time (single "Freie Objekte" page) | **Keep** as is (1 request per second, one list page and a few detail pages). |
| **JUWO** (juwo.ch) | Not verified | Not found | Offers go **by email** to registered, eligible applicants (age ≤ 28, income ≤ CHF 30k; [Kanton ZH](https://studienwahl.zh.ch/vorbereitung-studium-und-zwischenjahr/wg-zimmer-in-zuerich/)) | Registration/eligibility; no public listing page was found | ~3,000–4,000 rooms total, few vacancies at a time | **Don't add.** If eligible, the user subscribes to the JUWO emails manually. |
| **students.ch** | Not verified | Not found (no AGB text in search results) | None found | Public list view exists ([/wohnen/list?type=wg](https://www.students.ch/wohnen/list?type=wg)). Contact may need membership (unverified) | Tens of Zürich WG rooms (estimate) | **Ask the site.** Not added until robots.txt and AGB are read and allow it. |
| **WGZimmer** (wgzimmer.ch), adapter present but disabled | Not verified | Not found | None | **reCAPTCHA on search** (known; see `wgzimmer_adapter.py`) | Largest Swiss WG board (hundreds in Zürich, estimate) | **Don't add.** The captcha is an explicit "no". Could ask the operator for a feed. |
| **Ronorp** (ronorp.net), adapter off | Disallows `?page=` (as recorded in `ronorp_adapter.py`) | Not found | None | None | 0 in the Zürich WG category as of 2026-10-18 | **Leave off** (unchanged). |
| **Flatfox** (flatfox.ch), already used | Not verified (blocked) | No explicit scraping or bot clause appears in the excerpts found. The [GTC](https://flatfox.ch/c/en/gtc/) grant is narrow: "an unremunerated, non-exclusive right that is limited to the period of the contractual relationship … to use the real estate platform and the information and functions contained or available on it", and "users have no contractual rights to use the real estate platform" | The JSON endpoint used is the one the site's own map uses. It is **undocumented** (an archived `flatfox-api-python` repo exists on [github.com/flatfox-ag](https://github.com/flatfox-ag)) | None | Thousands of Swiss listings, hundreds in Zürich | **Ask the site.** Permission is unclear rather than refused. Recommend emailing Flatfox to confirm that low-rate personal use of the public-listing endpoint is fine. |
| **Homegate** (homegate.ch), **already used** | Not verified | **Prohibits it.** SMG GTC §7: "it is prohibited to systematically select the Content available on the Marketplaces (e.g. by scraping), to copy, publish or otherwise reproduce it … or to link it with other data" ([SMG GTC, real estate](https://homegate.ch/c/en/about-us/legal-issues/gbc)). The clause was quoted via search, not read directly | No public API (partner feeds only) | Third-party guides report anti-bot challenges | Thousands in Zürich | **Don't add, and reconsider the existing adapter** (see below). |
| **ImmoScout24** (immoscout24.ch) | Not verified | Same SMG GTC; the copyright and use wording also appears in the [ImmoScout24 GTC](https://www.immoscout24.ch/c/en/about-us/gtc) | No public API | Reported anti-bot | Thousands | **Don't add.** |
| **Comparis** (comparis.ch) | Not verified | [Nutzungsbestimmungen](https://www.comparis.ch/authentication/Info/Nutzungsbestimmungen): "Alle von Comparis auf dem Netz publizierten Informationen und Inhalte sind urheberrechtlich geschützt", and the user agrees "die angebotenen Inhalte und Informationen weder zu kopieren, …" (snippet cut off) | None | **DataDome** bot protection, deployed against scraping ([case study](https://datadome.co/customers-stories/comparis-automates-scraping-protection-saves-a-day-per-week-with-datadome/)) | Thousands (aggregator) | **Don't add** (an anti-bot wall plus restrictive terms). |
| **tutti.ch** | Not verified | Not found. Reported to be part of SMG (unverified), so similar terms are likely | Undocumented GraphQL only | Cloudflare managed challenge (reported by a third-party scraper author) | Some Zürich rooms | **Don't add** (anti-bot challenge). |
| **anibis.ch** | Not verified | Not found | None found | Unknown | Few | **Don't add** (unclear, low value). |
| **newhome.ch** | Not verified | [AGB (2017)](https://www.newhome.ch/~/de/mediaserve/1690/01_AGB_07_2017_DE_FINAL.pdf): all rights to the platform content belong to newhome (copyright and trademark). Section 6, "Auswertung von Daten", was not visible in the results | None found | Unknown | Hundreds (mostly flats, cantonal-bank listings) | **Ask the site** at most. Mostly overlaps Homegate and Flatfox, so low value. |
| **weegee.ch** (WG aggregator) | Not verified | Not found | App only | Unknown | Claims "twice as many listings as any other site" (an aggregator of other boards) | **Don't add** (it re-aggregates other boards, and its terms are unknown). |

## Finding on an existing source: Homegate

The SMG terms quoted above (§7) explicitly forbid systematic selection of marketplace content "e.g. by scraping". The Homegate adapter reads up to 40 result pages per run. That conflicts with the guardrail "obey robots.txt and each site's ToS". The clause was seen in search results, not on the live page. The owner should read <https://www.homegate.ch/c/en/about-us/legal-issues/gbc> §7 and decide whether to turn Homegate off by default, as was done for Ronorp. This review does not change any code.

## Recommendation

No candidate was found that clearly permits automated access and would add meaningful Zürich volume. In order of usefulness:

1. **Flatfox (keep; confirm).** Already the main source. Its terms contain no explicit ban in the excerpts found, but its JSON endpoint is undocumented. Ask Flatfox (via the contact on flatfox.ch) whether low-rate, personal, non-commercial use of the public-listing endpoint is acceptable. Until then, keep the current polite pattern: tiled pin requests, detail fetches only for new or changed ads, and at most about 1 request per second.
2. **WOKO (keep).** robots.txt allowed everything when the adapter was written. It is one public list page plus a few detail pages at 1 request per second, and is low risk.
3. **students.ch (ask before adding).** It is the only student board with a public list view. If robots.txt allows `/wohnen/list` and `/habitation/details/` and the AGB has no ban, an adapter could read the WG list for Zürich once per ingest run, plus new detail pages only, at 1 request per second. Otherwise, email the operator.
4. **UZH Alumni Marktplatz (ask).** Low volume; the same polite pattern would apply if allowed.
5. **ETH/UZH Housing Portal, JUWO:** use them manually (login or email alerts). They are not automatable under the guardrails.
6. **Homegate:** reconsider (see above). **ImmoScout24, Comparis, tutti, WGZimmer:** don't add (terms or anti-bot measures).

## Not verified (must be checked from a normal connection before any adapter work)

- Every robots.txt except the WOKO and Ronorp notes already in the code (and those were not re-read today).
- The full current text of the Flatfox, students.ch, WGZimmer, JUWO, Ronorp, anibis, tutti, newhome (§6) and weegee terms.
- Whether the UZH Alumni Marktplatz is still public, and whether students.ch hides contact details behind a login.
- All listing counts, which are estimates from search results.
