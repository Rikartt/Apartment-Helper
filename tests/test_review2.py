"""Regression tests from the review2 pass: service worker shell, access redirect, ntfy click
header, sent/replied interplay, and migration of an old-schema database."""
import sqlite3
from pathlib import Path

import httpx

from backend import notify
from tests.test_api import seeded  # noqa: F401  (fixture)
from tests.conftest import make_ad

FRONT = Path(__file__).resolve().parent.parent / "frontend"


def test_sw_precaches_every_script_index_loads():
    sw = (FRONT / "sw.js").read_text()
    shell = sw.split("SHELL_URLS = [")[1].split("]")[0]
    for f in ("/pwa.js", "/checklist.js"):
        assert f in shell
    html = (FRONT / "index.html").read_text()
    assert 'src="/pwa.js"' in html and 'src="checklist.js"' in html


def test_sw_does_not_cache_arbitrary_navigations_as_the_shell():
    sw = (FRONT / "sw.js").read_text()
    assert "wgp-shell-v1" not in sw            # cache version bumped with the logic
    assert "isPage && url.pathname === '/'" in sw
    assert "{ cache: 'no-store' }" in sw       # API calls bypass the HTTP cache too


def test_guard_redirect_stays_on_origin(client, monkeypatch):
    monkeypatch.setenv("WGP_ACCESS_TOKEN", "s3cret")
    r = client.get("//evil.example/x?token=s3cret", follow_redirects=False)
    assert r.status_code == 303
    assert not r.headers["location"].startswith("//")


def test_guard_covers_docs_and_openapi(client, monkeypatch):
    monkeypatch.setenv("WGP_ACCESS_TOKEN", "s3cret")
    for p in ("/docs", "/openapi.json", "/redoc", "/profile", "/frontend/index.html",
              "/sw.js", "/pwa.js", "/checklist.js", "/notify/status"):
        assert client.get(p).status_code == 401, p
    assert client.post("/notify/test").status_code == 401
    assert client.post("/draft/1").status_code == 401


def test_ntfy_click_header_is_ascii_safe(monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw)
        kw["headers"]["Click"].encode("ascii")     # httpx would raise on non-ASCII
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    notify._send_ntfy("t", "b", "https://ex.test/wohnung/zürich?a=1&b=ä")
    assert "z%C3%BCrich" in seen["headers"]["Click"] and "?a=1&b=" in seen["headers"]["Click"]


def test_first_message_does_not_downgrade_replied(client, seeded):
    client.post("/listings/1/status", json={"status": "replied"})
    r = client.post("/listings/1/sent", json={"text": "late note"})
    assert r.json()["listing"]["status"] == "replied"
    assert len(client.get("/listings/1").json()["messages"]) == 1


def test_first_message_after_skip_marks_sent(client, seeded):
    client.post("/listings/1/status", json={"status": "rejected"})
    assert client.post("/listings/1/sent", json={"text": "x"}).json()["listing"]["status"] == "sent"


def test_followup_prompt_language_falls_back_to_first_message(monkeypatch):
    from backend import drafting
    captured = {}

    class Msg:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": "ok"})()]

    class Client:
        class beta:
            class messages:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return Msg()
    monkeypatch.setattr(drafting.llm, "get_client", lambda: Client)
    drafting.draft({"title": "t", "address": "a"}, {}, followup_of="Hallo zusammen")
    assert "language as the applicant's first message" in captured["system"]


OLD_SCHEMA = """CREATE TABLE listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, external_id TEXT NOT NULL,
    title TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL,
    rent_chf INTEGER NOT NULL, available_from TEXT NOT NULL, raw_text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', bike_zentrum_min INTEGER, transit_zentrum_min INTEGER,
    bike_hoenggerberg_min INTEGER, transit_hoenggerberg_min INTEGER,
    seen INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
    replied INTEGER NOT NULL DEFAULT 0, extracted TEXT, fit_score INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), listed_at TEXT, reply_score INTEGER,
    UNIQUE(source, external_id))"""


def test_old_schema_db_migrates_and_keeps_sent_replied(tmp_path, monkeypatch):
    from backend import db as dbm
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute(OLD_SCHEMA)
    for i, (s, r) in enumerate([(1, 0), (1, 1), (0, 0)]):
        c.execute("INSERT INTO listings (source, external_id, title, address, lat, lng, rent_chf, "
                  "available_from, sent, replied, extracted) VALUES ('x',?,?,'a',47.3,8.5,800,'now',?,?,?)",
                  (str(i), f"t{i}", s, r, '{"language": "de"}' if i == 0 else None))
    c.commit()
    c.close()
    monkeypatch.setattr(dbm, "DB_PATH", path)
    dbm.init_db()
    dbm.init_db()                                   # idempotent
    rows = {r["external_id"]: r for r in dbm.get_all_listings(False)}
    assert [rows[k]["status"] for k in "012"] == ["sent", "replied", None]
    assert rows["0"]["extracted"] == {"language": "de"}
    assert rows["0"]["kind"] == "wg_room" and rows["0"]["notified_at"] is None
    assert dbm.add_sent_message(rows["2"]["id"], "hi")["kind"] == "first"
    assert dbm.get_stats()["counts"]["sent"] == 2
    # an ingest on the migrated DB still works and leaves the user's status alone
    assert dbm.upsert_listing(make_ad(source="x", external_id="0")) is False
    assert dbm.get_listing(rows["0"]["id"])["status"] == "sent"
