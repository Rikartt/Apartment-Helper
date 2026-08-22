# WGPENDLER — build brief for Claude Code

## How to use this file
Paste this whole file into Claude Code as the opening message. Tell it: **"Read this brief, propose a file structure and a phased plan, then wait for my go-ahead before writing code. Build one phase at a time and let me review after each."** Don't let it one-shot the whole thing — the extraction and drafting prompts are the parts worth getting right.

---

## Context: what already exists
I have a working single-file frontend prototype, `wgpendler.html` (I'll drop it in the repo). It's a Leaflet map of Zürich that shows WG room listings and filters them by rent range and real commute time to ETH Zürich (two campuses: Zentrum and Hönggerberg), by public transport or bike. It already has the visual language I want — a Swiss transit-console look, departure-board monospace numbers, red ETH anchor nodes, blue = transit line, green = bike line. **Keep that exact aesthetic.** Right now it runs on ~14 hardcoded sample listings.

## The goal
Turn it into a tool that raises my **reply rate per message** when hunting for a room, so writing to ~10 well-chosen flats beats spraying 100. The tool should:
1. Ingest room ads from permitted sources.
2. Read each ad and extract its real requirements, preferences, and vibe.
3. Filter out ads I can't win (dealbreakers), rank the rest by fit-to-me × freshness.
4. For a good-fit ad, draft a short, specific, authentic opener **in the ad's language** that I review and send myself.

This is a **human-in-the-loop assistant, not an auto-sender.** See Guardrails.

---

## Architecture
- **Backend:** Python + FastAPI. Responsibilities: fetch listings from permitted feeds, geocode addresses, compute commute times, run the Claude extraction pass, score fit in code, store everything, expose a JSON API, and a `/draft` endpoint.
- **Store:** SQLite (dedupe by source + ad ID/URL; track `seen`, `sent`, `replied` per ad).
- **Frontend:** extend the existing `wgpendler.html`. Add: a fit score + match-reason chips on each card, ranking by fit × recency, a one-time "My profile" panel, and a **"Draft message"** button per card that calls `/draft` and shows an editable message I copy out.
- **Model calls:** Anthropic API via the official `anthropic` Python SDK. Key from `ANTHROPIC_API_KEY` env var — never hardcode it. Use `claude-sonnet-5` for drafting; `claude-haiku-4-5-20251001` is fine and cheaper for the bulk extraction pass.

## Phased plan (build and let me review each phase)
1. **Skeleton:** FastAPI app, SQLite schema, serve the existing frontend, one `/listings` endpoint returning the current sample data as JSON. Frontend reads from the endpoint instead of the hardcoded array. Nothing else changes visually.
2. **Ingestion:** a fetcher module with one adapter per permitted source (start with a mock adapter that reads a local `sample_ads.json` of raw ad text, so we can build the pipeline before touching real sites). Geocode + commute-time computation move server-side.
3. **Extraction:** for each ingested ad, call Claude with the extraction prompt below, store the structured result.
4. **Fit scoring (in code, not the model):** given my profile + the extracted fields, filter dealbreakers and compute a 0–100 fit score. Re-rank the list.
5. **Drafting:** `/draft` endpoint + the "Draft message" button, using the drafting prompt below.
6. **Outcome logging:** mark ads `sent`/`replied`; store it so fit scoring can later learn from what actually got replies.

---

## Data: my profile (fill in real values; keep it as `profile.json`)
```json
{
  "status": "incoming ETH Zürich MSc student, Electrical Engineering, starting September",
  "move_in_earliest": "2026-09-01",
  "duration": "long-term / permanent",
  "budget_chf": {"min": 0, "max": 1100},
  "languages": [{"lang": "English", "level": "native"}, {"lang": "German", "level": "B1"}],
  "smoker": false,
  "gender": "…",
  "age": 22,
  "traits": ["tidy", "quiet weeknights", "social on weekends", "cook", "play five-a-side football"],
  "wants": ["long-term room", "a WG that actually hangs out", "near a tram/train to ETH"],
  "has": ["stable funding / can show proof", "references available"]
}
```

## Data: extracted ad fields (the extraction pass fills this per ad)
```json
{
  "language": "de | en | other",
  "rent_chf": 0,
  "move_in": "YYYY-MM-DD or 'flexible'",
  "duration": "permanent | fixed_term | sublet",
  "hard_requirements": ["e.g. non-smoker", "woman only", "German-speaking", "working professional"],
  "preferences": ["e.g. social household", "shared Sunday dinners", "clean"],
  "vibe": ["3-word household character tags"],
  "household_size": 0,
  "asks_applicant_to_include": ["e.g. short intro", "move-in date", "who you are"],
  "red_flags": ["deposit before viewing", "too-good price", "vague listing"]
}
```

---

## PROMPT 1 — extraction (use for each ad; ask for JSON only)
System:
```
You extract structured facts from a shared-flat (WG) rental ad. Return ONLY valid JSON
matching the schema the user provides — no prose, no markdown fences. If a field isn't
stated in the ad, use null or an empty array. Do not infer facts that aren't there.
Detect the ad's language. Separate HARD requirements (things the poster says an applicant
must be/have) from soft PREFERENCES (things they'd like). Flag anything that reads like a
scam (payment before viewing, implausible price, no address).
```
User (per ad):
```
SCHEMA:
{ "language": "...", "rent_chf": ..., "move_in": "...", "duration": "...",
  "hard_requirements": [...], "preferences": [...], "vibe": [...],
  "household_size": ..., "asks_applicant_to_include": [...], "red_flags": [...] }

AD:
<<<
{raw ad text here}
>>>
```

## Fit scoring (do this in code from the extracted fields — cheaper and more predictable than asking the model)
- **Dealbreaker filter:** drop or grey out an ad if any `hard_requirement` conflicts with my profile (e.g. "woman only" and I'm not; "German-speaking" and my German is below their bar; wants a working professional not a student; fixed-term sublet when I need permanent; move-in far from mine).
- **Score (0–100):** start at a base, add points for each `preference`/`vibe` tag my `traits`/`wants` satisfy, add a recency bonus (newer ad = higher; decay over days), add a small bonus if rent is comfortably inside my budget. Subtract for `red_flags`.
- **Rank** surviving ads by score. Surface the matched tags as "why this fits" chips on the card.

## PROMPT 2 — drafting (per ad, on demand)
System:
```
You draft a short first message from a room-seeker to a WG poster. Goals: sound like a
real, specific person who clearly read THIS ad, and earn a viewing. Rules:
- Write in the ad's language ({language}). If it's German and my level is B1, keep it
  simple and natural, not florid.
- Reference 1–2 concrete details from the actual ad (their stated vibe, a shared interest,
  the neighbourhood, what they asked applicants to include).
- Use ONLY facts from my profile. Never invent language ability, hobbies, or claims.
- 4–7 sentences. Warm, direct, not gushing. Include: who I am, why I fit what they said,
  my move-in date and that I want long-term, and a clear ask to view the room.
- Include anything they explicitly asked applicants to state.
- Output only the message text.
```
User:
```
MY PROFILE: {profile.json}
THE AD (raw): <<< {raw ad text} >>>
EXTRACTED: {extracted fields json}
```

---

## Guardrails (these are part of what makes it work — not optional)
- **Draft-and-review only. Never auto-send.** The tool hands me an editable message; I send it myself. No headless posting/messaging on any platform — it's spammy, breaks platform ToS, and WGs can smell a bot, which kills the reply rate this whole thing exists to raise.
- **Respect each source.** Use official RSS/feeds/APIs where they exist; obey robots.txt and each site's ToS; rate-limit fetching politely. **Do not scrape Facebook** (no public API, against ToS, gets accounts banned) — I'll use Facebook's own in-group keyword alerts manually.
- **No fabrication in drafts.** Only real profile facts. If a field is empty, omit it rather than invent.
- **Secrets in env vars.** `ANTHROPIC_API_KEY` from the environment; nothing sensitive committed.
- **Keep the frontend aesthetic** from `wgpendler.html` exactly.

## Done-looks-like
- I run the backend, open the map, and see ads ranked by fit with "why this fits" chips and both commute times.
- Dealbreaker ads are filtered out or clearly greyed.
- Clicking "Draft message" gives me a tailored, in-language opener I can edit and copy in ~10 seconds.
- I can mark an ad sent/replied, and that's stored for later tuning.

## First thing to do
Propose the repo structure and the Phase 1 plan, then wait for my confirmation.
