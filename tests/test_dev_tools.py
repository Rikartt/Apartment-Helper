"""tools/dev_seed.py and the dev server's fake LLM (offline, no browser)."""
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import dev_seed  # noqa: E402
import dev_server  # noqa: E402


def test_seed_builds_realistic_db(tmp_path):
    from backend import db as dbm, trust
    before = dbm.DB_PATH
    info = dev_seed.build(tmp_path / "dev.db", n=80, outcomes=True)
    assert dbm.DB_PATH == before                       # global restored
    assert info["listings"] == 80 and info["scams"] == 2
    assert set(info["kinds"]) == {"wg_room", "studio", "apartment"}
    assert info["outcomes"]["sent"] > 0 and info["outcomes"]["replied"] > 0

    dbm.DB_PATH = tmp_path / "dev.db"
    try:
        rows = dbm.get_all_listings()
        assert all(r["fit_score"] is not None and r["reply_score"] is not None for r in rows)
        assert any(r["extracted"] for r in rows)
        assert {(r["extracted"] or {}).get("language") for r in rows} >= {"de", "en"}
        trust.annotate(rows)
        low = [r for r in rows if r["trust_level"] == "low"]
        assert len(low) == 2 and all(r["rent_chf"] == 390 for r in low)   # only the scams
        assert dbm.recent_runs(1)[0]["ok"]
    finally:
        dbm.DB_PATH = before


def test_seed_is_deterministic_and_refuses_overwrite(tmp_path):
    a = dev_seed.build(tmp_path / "a.db", n=30, seed=3)
    b = dev_seed.build(tmp_path / "b.db", n=30, seed=3)
    assert a["kinds"] == b["kinds"] and a["extracted"] == b["extracted"]
    with pytest.raises(FileExistsError):
        dev_seed.build(tmp_path / "a.db", n=30)
    dev_seed.build(tmp_path / "a.db", n=10, force=True)


def test_fake_llm_answers_every_call_site():
    from backend import drafting
    fake = dev_server.FakeAnthropic()
    warm = fake.beta.messages.create(system=drafting.build_system_prompt("de", "warm", "auto"), messages=[])
    short = fake.beta.messages.create(system=drafting.build_system_prompt("de", "short", "auto"), messages=[])
    en = fake.beta.messages.create(system=drafting.build_system_prompt("en", "warm", "auto"), messages=[])
    texts = [m.content[0].text for m in (warm, short, en)]
    assert all(dev_server.FAKE_TAG in t for t in texts)
    assert len(texts[1].split()) < len(texts[0].split())
    assert texts[0].startswith("Hallo") and texts[2].startswith("Hi")
    ext = fake.messages.create(messages=[{"role": "user", "content": "SCHEMA:\n{}\n\nAD:\nWir sind eine WG"}])
    assert json.loads(ext.content[0].text)["language"] == "de"
    best = fake.messages.create(messages=[{"role": "user", "content":
                                           "LISTING 1 (id=7):\n...\nReturn ONLY the JSON array."}])
    assert json.loads(best.content[0].text)[0]["id"] == 7
    assert len(fake.calls) == 5
