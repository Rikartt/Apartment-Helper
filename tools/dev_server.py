"""Run WGPendler on a synthetic database, fully offline, for UI work and browser checks.

    python tools/dev_server.py                       # seeds ~200 listings, http://127.0.0.1:8765
    python tools/dev_server.py --fake-llm --outcomes # canned drafts/summaries, sent/replied history
    python tools/dev_server.py --clear-template      # empty message_template: walkthrough shows
    python tools/dev_server.py --group               # a 3-person group profile for group mode
    python tools/dev_server.py --db /tmp/x.db        # reuse (or create) a specific DB

What it isolates (nothing touches your real wgpendler.db, profile.json, backups/ or geocache):
- DB: --db, or <work>/dev.db, seeded with tools/dev_seed.py when it does not exist yet.
- Profile: a copy of the repo's profile.json in <work>/profile.json (edits in the UI land there).
- No ingest: the startup and periodic ingests are off and run_ingest is replaced by a no-op,
  so even POST /ingest makes no network request. No phone alerts, no access token.
- Backups go to <work>/backups, the geocache to <work>/geocache.sqlite.
- Anthropic: a real client is NEVER constructed (anthropic.Anthropic is replaced by a function
  that raises). Without --fake-llm the key is empty, so Claude features answer 503 like a
  fresh install. With --fake-llm a stand-in client returns canned drafts, follow-ups,
  summaries, Top 5 analyses and extractions, so every draft UI can be exercised for free.

<work> is --work, or a new temp dir. Prints "WGPendler dev server ready on <url>" once it
accepts requests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FAKE_TAG = "[fake LLM]"

SAMPLE_GROUP = {
    "size": 3,
    "budget_per_person_chf": 1300,
    "members": [
        {"label": "me", "campus": "zentrum"},
        {"label": "friend A", "campus": "hoenggerberg", "budget_chf": 1150},
        {"label": "friend B", "campus": "zentrum"},
    ],
    "notes": "Three ETH students (EE, CS, ME), non-smokers, looking for a long-term flat.",
}


# ── fake Anthropic client ───────────────────────────────────────────────────────
class _Msg:
    def __init__(self, text: str):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = "end_turn"
        self.usage = types.SimpleNamespace(input_tokens=0, output_tokens=0)


def _fake_reply(kw: dict) -> str:
    system = str(kw.get("system") or "")
    user = "\n".join(str(m.get("content", "")) for m in kw.get("messages") or [])
    low = system.lower()
    if user.startswith("SCHEMA:"):       # extraction
        de = bool(re.search(r"\b(wir|und|zimmer|ich)\b", user, re.I))
        return json.dumps({"language": "de" if de else "en", "rent_chf": None, "move_in": "flexible",
                           "duration": "permanent", "poster_type": "private",
                           "hard_requirements": ["non-smoker"], "preferences": ["tidy"],
                           "vibe": ["social"], "household_size": 3,
                           "asks_applicant_to_include": ["short intro", "move-in date"], "red_flags": []})
    if "Return ONLY the JSON array" in user:   # Top 5 analysis
        ids = [int(x) for x in re.findall(r"\(id=(\d+)\)", user)]
        return json.dumps([{"id": i, "strengths": ["fits the budget", "short commute"],
                            "weaknesses": ["fake analysis"], "tip": f"{FAKE_TAG} mention your move-in date"}
                           for i in ids])
    if "summar" in low:
        return f"{FAKE_TAG} Bright room in a 3-person WG, long-term, non-smokers wanted."
    if "follow-up" in low:   # in the language the prompt names (the first message's, else the ad's)
        if "- Write in English" in system or "- Write in the ad's language (English)" in system:
            return (f"Hello again,\n\nI just wanted to check whether the room is still available. "
                    f"I am still very interested.\n\nBest regards\n{FAKE_TAG}")
        return (f"Hallo nochmal,\n\nich wollte kurz nachfragen, ob das Zimmer noch frei ist. "
                f"Ich hätte weiterhin grosses Interesse.\n\nViele Grüsse\n{FAKE_TAG}")
    german = "German" in system and "English (the applicant chose" not in system
    if "group" in low:
        if "SHORT version" in system:
            return (f"Guten Tag, wir sind drei ETH-Studierende und möchten die Wohnung mit Balkon gemeinsam mieten; "
                    f"alle würden den Mietvertrag unterschreiben. Wir sind ab sofort verfügbar. Dürfen wir sie "
                    f"besichtigen? Freundliche Grüsse {FAKE_TAG}"
                    if german else
                    f"Hello, we are three ETH students who would like to rent your apartment with the balcony "
                    f"together; all of us would sign the lease. We are available immediately. Could we view it? "
                    f"Best regards {FAKE_TAG}")
        return (f"Guten Tag\n\nwir sind eine Gruppe von drei ETH-Studierenden und suchen eine Wohnung für längere "
                f"Zeit. Ihre Wohnung mit Balkon und der Lage nahe der ETH passt sehr gut zu uns. Wir möchten als "
                f"Wohngemeinschaft zusammen wohnen und sind offen, das Mietverhältnis mit Ihnen zu besprechen, zum "
                f"Beispiel dass alle den Mietvertrag unterschreiben. Wir sind ab sofort verfügbar. Hätten Sie Zeit "
                f"für eine Besichtigung?\n\nFreundliche Grüsse\n{FAKE_TAG}"
                if german else
                f"Hello,\n\nwe are a group of three ETH students looking for a long-term flat, and your "
                f"apartment with the balcony and its location near ETH fits us well. We would like to live together "
                f"as a flat-share and are happy to discuss the tenancy arrangement, for example that all of us sign "
                f"the lease. We are available immediately. Could we arrange a viewing?\n\nBest regards\n{FAKE_TAG}")
    if "SHORT version" in system:
        return (f"Hallo, ich bin ETH-Student, mag eure gemeinsamen Abendessen und könnte ab nächstem Monat "
                f"langfristig einziehen. Darf ich das Zimmer besichtigen? Viele Grüsse {FAKE_TAG}"
                if german else
                f"Hi, I'm an ETH student, I like that you share dinners, and I could move in next month "
                f"for the long term. Could I come to see the room? Best {FAKE_TAG}")
    return (f"Hallo zusammen,\n\nich bin Student an der ETH und suche ein Zimmer für längere Zeit. Eure "
            f"Beschreibung der gemeinsamen Abendessen gefällt mir sehr: ich koche gern und bin unter der "
            f"Woche eher ruhig. Einziehen könnte ich ab nächstem Monat. Hättet ihr Zeit für eine "
            f"Besichtigung?\n\nViele Grüsse\n{FAKE_TAG}"
            if german else
            f"Hi all,\n\nI'm an ETH student looking for a long-term room. I like that you share dinners: "
            f"I enjoy cooking and I'm quiet during the week. I could move in next month. Would you have "
            f"time for a viewing?\n\nBest regards\n{FAKE_TAG}")


class _FakeMessages:
    def __init__(self, log: list):
        self._log = log

    def create(self, **kw):
        self._log.append(kw.get("model"))
        return _Msg(_fake_reply(kw))


class FakeAnthropic:
    """Duck-types the bits of anthropic.Anthropic the backend uses (messages / beta.messages)."""

    def __init__(self):
        self.calls: list = []
        self.messages = _FakeMessages(self.calls)
        self.beta = types.SimpleNamespace(messages=_FakeMessages(self.calls))


# ── setup ─────────────────────────────────────────────────────────────────────
def prepare(work: Path, db: Path | None, clear_template: bool, group: bool, outcomes: bool,
            fake_llm: bool, seed: int, n: int):
    """Configure env + backend modules for an isolated, offline run. Returns backend.main."""
    work.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "WGP_INGEST_INTERVAL_MIN": "0", "WGP_AUTO_EXTRACT": "0",
        "WGP_BACKUP_DIR": str(work / "backups"), "WGPENDLER_GEOCACHE": str(work / "geocache.sqlite"),
        # set (even empty) before backend.main runs load_dotenv(), so a real key in .env is ignored
        "ANTHROPIC_API_KEY": "fake-llm-no-network" if fake_llm else "",
    })
    for k in ("WGP_NTFY_TOPIC", "WGP_TELEGRAM_TOKEN", "WGP_TELEGRAM_CHAT_ID", "WGP_ACCESS_TOKEN"):
        os.environ.pop(k, None)

    import anthropic
    from backend import db as dbm, scoring, llm

    def _no_real_client(*a, **k):
        raise RuntimeError("dev_server: constructing a real Anthropic client is blocked")
    anthropic.Anthropic = _no_real_client
    anthropic.Client = _no_real_client
    fake = FakeAnthropic() if fake_llm else None
    if fake:
        llm.get_client = lambda: fake
    else:
        def _no_key():
            raise llm.LLMError("ANTHROPIC_API_KEY is not set (dev server without --fake-llm).", 503, True)
        llm.get_client = _no_key

    prof_path = work / "profile.json"
    profile = json.loads((ROOT / "profile.json").read_text())
    if clear_template:
        profile["message_template"] = ""
    if group:
        profile["group"] = json.loads(json.dumps(SAMPLE_GROUP))
    prof_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2))
    scoring.PROFILE_PATH = prof_path

    db = db or work / "dev.db"
    if not db.exists():
        import dev_seed
        info = dev_seed.build(db, n=n, seed=seed, outcomes=outcomes, profile=profile)
        print(f"[dev] seeded {info['listings']} listings into {db}", flush=True)
    dbm.DB_PATH = db

    import backend.main as m
    m.PROFILE_PATH = prof_path
    m._startup_ingest = lambda: print("[dev] startup ingest skipped", flush=True)

    def _no_ingest(sources=None, *a, **k):
        print(f"[dev] ingest of {sources} skipped (dev server is offline)", flush=True)
        return {"total": 0, "new": 0, "gone": 0, "sources": {}}
    m.run_ingest = _no_ingest
    m._dev_fake_llm = fake
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run WGPendler offline on a synthetic DB.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--work", help="folder for the DB, profile copy, backups (default: new temp dir)")
    ap.add_argument("--db", help="SQLite file to use; seeded if it does not exist")
    ap.add_argument("--clear-template", action="store_true",
                    help="empty message_template in the profile copy (first-run walkthrough shows)")
    ap.add_argument("--group", action="store_true", help="add a sample 3-person group to the profile copy")
    ap.add_argument("--outcomes", action="store_true", help="seed sent/replied history (new DBs only)")
    ap.add_argument("--fake-llm", action="store_true", help="canned Claude answers, zero API calls")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--n", type=int, default=200, help="listings to seed (new DBs only)")
    a = ap.parse_args(argv)
    work = Path(a.work).resolve() if a.work else Path(tempfile.mkdtemp(prefix="wgp_dev_"))
    db = Path(a.db).resolve() if a.db else None
    if db and db == (ROOT / "wgpendler.db").resolve():
        ap.error("refusing to run on the real wgpendler.db")
    m = prepare(work, db, a.clear_template, a.group, a.outcomes, a.fake_llm, a.seed, a.n)

    import uvicorn
    url = f"http://{a.host}:{a.port}"
    config = uvicorn.Config(m.app, host=a.host, port=a.port, log_level="warning")
    server = uvicorn.Server(config)
    orig_startup = server.startup

    async def startup(*args, **kw):
        await orig_startup(*args, **kw)
        print(f"[dev] work dir {work}  fake LLM: {'on' if a.fake_llm else 'off'}", flush=True)
        print(f"WGPendler dev server ready on {url}", flush=True)
    server.startup = startup
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
