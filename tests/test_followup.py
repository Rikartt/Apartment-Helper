"""Sent-message history, follow-up due logic and the follow-up draft prompt (all offline)."""
from datetime import datetime, timedelta, timezone

import backend.main as m
from backend import drafting
from tests.test_api import seeded  # noqa: F401  (fixture)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _backdate(db, lid, days):
    with db.get_conn() as c:
        c.execute("UPDATE sent_messages SET sent_at = ? WHERE listing_id = ?", (_ago(days), lid))
        c.execute("UPDATE listings SET status_updated_at = ? WHERE id = ?", (_ago(days), lid))


def test_sent_stores_text_and_marks_sent(client, seeded):
    r = client.post("/listings/1/sent", json={"text": "  Hallo!  "})
    assert r.status_code == 200
    body = r.json()
    assert body["message"]["text"] == "Hallo!" and body["listing"]["status"] == "sent"
    d = client.get("/listings/1").json()
    assert [x["text"] for x in d["messages"]] == ["Hallo!"]
    assert d["sent_at"] and d["followup_due"] is False


def test_sent_validation(client, seeded):
    assert client.post("/listings/1/sent", json={"text": "  "}).status_code == 422
    assert client.post("/listings/999/sent", json={"text": "x"}).status_code == 404
    assert client.post("/listings/1/sent", json={"text": "x", "kind": "bogus"}).status_code == 422


def test_history_survives_clearing_status(client, seeded):
    client.post("/listings/1/sent", json={"text": "Hi"})
    client.post("/listings/1/status", json={"status": None})
    assert len(client.get("/listings/1").json()["messages"]) == 1


def test_followup_due_after_threshold(client, seeded, monkeypatch):
    client.post("/listings/1/sent", json={"text": "Hi"})
    by = lambda: {l["id"]: l for l in client.get("/listings").json()}
    assert by()[1]["followup_due"] is False
    _backdate(seeded, 1, 4)
    assert by()[1]["followup_due"] is True
    monkeypatch.setenv("WGP_FOLLOWUP_DAYS", "10")
    assert by()[1]["followup_due"] is False
    monkeypatch.setenv("WGP_FOLLOWUP_DAYS", "garbage")   # falls back to 3
    assert by()[1]["followup_due"] is True


def test_replied_or_skipped_never_due(client, seeded):
    client.post("/listings/1/sent", json={"text": "Hi"})
    _backdate(seeded, 1, 9)
    client.post("/listings/1/status", json={"status": "replied"})
    assert client.get("/listings/1").json()["followup_due"] is False


def test_legacy_sent_without_message_uses_status_time(client, seeded):
    client.post("/listings/2/status", json={"status": "sent"})
    with seeded.get_conn() as c:
        c.execute("UPDATE listings SET status_updated_at = ? WHERE id = 2", (_ago(5),))
    assert client.get("/listings/2").json()["followup_due"] is True


def test_followup_message_restarts_timer_and_keeps_status(client, seeded):
    client.post("/listings/1/sent", json={"text": "first"})
    _backdate(seeded, 1, 6)
    assert client.get("/listings/1").json()["followup_due"] is True
    r = client.post("/listings/1/sent", json={"text": "nudge", "kind": "followup"})
    assert r.json()["listing"]["status"] == "sent"
    assert client.get("/listings/1").json()["followup_due"] is False


def test_delete_sent_message(client, seeded):
    mid = client.post("/listings/1/sent", json={"text": "Hi"}).json()["message"]["id"]
    assert client.delete(f"/listings/1/sent/{mid}").status_code == 200
    assert client.delete(f"/listings/1/sent/{mid}").status_code == 404
    assert client.get("/listings/1").json()["messages"] == []


def test_followup_draft_needs_stored_first_message(client, seeded, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert client.post("/draft/1?followup=1").status_code == 409


def test_followup_draft_passes_first_message(client, seeded, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = {}
    def fake(listing, profile, followup_of=None):
        seen["f"] = followup_of
        return "Kurz nachgefragt."
    monkeypatch.setattr(m, "draft", fake)
    client.post("/listings/1/sent", json={"text": "Hallo, ich bin Rikard."})
    r = client.post("/draft/1?followup=1").json()
    assert r["followup"] is True and r["draft"] == "Kurz nachgefragt."
    assert seen["f"] == "Hallo, ich bin Rikard."


def test_followup_prompt_is_short_polite_and_language_aware():
    system, user = drafting.build_followup_prompt(
        {"title": "WG Zimmer", "address": "Zürich"}, "Hallo, ich bin Rikard.", "German")
    assert "German" in system and "SHORT" in system and "Do NOT repeat the first message" in system
    assert "Hallo, ich bin Rikard." in user
    assert len(system) < 1200
