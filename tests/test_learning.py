"""Phase 6b: learning from outcomes. Synthetic data only, no network."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from backend import learning

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _fmt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def seed(db, n, replies_if_fresh=True, seed_=1):
    """n contacted listings sent 10+ days ago. Posted-to-sent gap is random; replies
    happen iff the message went out within 2 days of posting (recency drives replies)."""
    rnd = random.Random(seed_)
    plan = []
    for i in range(n):
        gap = rnd.choice([0.2, 0.5, 1.0, 5.0, 8.0, 12.0])
        sent = NOW - timedelta(days=rnd.uniform(10, 40))
        posted = sent - timedelta(days=gap)
        db.upsert_listing(dict(
            source=rnd.choice(["flatfox", "homegate"]), external_id=f"l{i}",
            title="t", address="Zürich", lat=47.37, lng=8.54,
            rent_chf=rnd.choice([700, 900, 1100]), available_from="2026-11-01",
            raw_text="x", url=f"https://e.test/{i}", listed_at=_fmt(posted)))
        plan.append((f"l{i}", gap, sent, rnd.randint(50, 90), rnd.randint(20, 150)))
    with db.get_conn() as conn:   # separate phase: upsert_listing opens its own connection
        for ext_id, gap, sent, fit, words in plan:
            lid = conn.execute("SELECT id FROM listings WHERE external_id = ?", (ext_id,)).fetchone()[0]
            replied = gap <= 2 if replies_if_fresh else False
            conn.execute("UPDATE listings SET status = ?, status_updated_at = ?, fit_score = ? "
                         "WHERE id = ?", ("replied" if replied else "sent",
                                          _fmt(sent + timedelta(hours=5)), fit, lid))
            conn.execute("INSERT INTO sent_messages (listing_id, kind, text, sent_at) "
                         "VALUES (?, 'first', ?, ?)", (lid, "hi " * words, _fmt(sent)))


def test_blend_weight_threshold_and_growth():
    assert learning.blend_weight(19, 20) == 0.0
    w20, w50, w500 = (learning.blend_weight(n, 20) for n in (20, 50, 500))
    assert 0 < w20 < w50 < w500 <= learning.MAX_BLEND


def test_min_outcomes_env(monkeypatch):
    assert learning.min_outcomes() == 20
    monkeypatch.setenv("WGP_LEARN_MIN_OUTCOMES", "5")
    assert learning.min_outcomes() == 5
    monkeypatch.setenv("WGP_LEARN_MIN_OUTCOMES", "junk")
    assert learning.min_outcomes() == 20


def test_below_threshold_is_noop(db):
    seed(db, 19)
    assert learning.train(now=NOW) is None
    assert learning.load_model() is None
    listing = {"rent_chf": 900, "listed_at": "2026-10-18", "transit_zentrum_min": 20}
    assert learning.adjust_reply(61, listing, {}, {}, NOW.date(), 70) == 61


def test_learns_recency_and_stores_weights(db):
    seed(db, 80)
    m = learning.train(now=NOW, profile={"budget_chf": {"max": 1200}})
    assert m and m["n"] == 80
    w = m["weights"]
    assert w["recency"] == max(w.values())     # strongest factor, and positive
    assert w["recency"] > 1.0
    stored = learning.load_model()
    assert stored["weights"] == pytest.approx(w)
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM learned_weights").fetchone()[0] == len(learning.FEATURES)
    x_fresh = learning.features(fit=70, rent=900, budget=1200, commute=20, age_days=0.2,
                                msg_len=300, ext={}, profile={})
    x_old = learning.features(fit=70, rent=900, budget=1200, commute=20, age_days=12,
                              msg_len=300, ext={}, profile={})
    assert learning.predict(m, x_fresh) > 0.65 and learning.predict(m, x_old) < 0.45


def test_adjust_reply_blends_after_threshold(db, monkeypatch):
    seed(db, 80)
    monkeypatch.setattr(learning, "_cache", {})
    learning.train(now=NOW, profile={"budget_chf": {"max": 1200}})
    prof = {"budget_chf": {"max": 1200}}
    fresh = {"rent_chf": 900, "listed_at": _fmt(datetime.now(timezone.utc)), "transit_zentrum_min": 20}
    old = dict(fresh, listed_at="2026-09-01")
    today = datetime.now(timezone.utc).date()
    assert learning.adjust_reply(50, fresh, {}, prof, today, 70) > 50 > \
        learning.adjust_reply(50, old, {}, prof, today, 70)


def test_pending_and_skipped_are_not_outcomes(db):
    seed(db, 3)
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET status = 'rejected' WHERE id = 1")
        conn.execute("UPDATE sent_messages SET sent_at = ? WHERE listing_id = 2",
                     (_fmt(NOW - timedelta(days=1)),))
        conn.execute("UPDATE listings SET status = 'sent' WHERE id = 2")
    by_id = {r["id"]: r["outcome"] for r in learning.collect(NOW)}
    assert 1 not in by_id and by_id[2] is None


def test_stats_endpoint_backward_compatible_and_extended(client, db):
    r = client.get("/stats").json()
    for k in ("total", "counts", "contacted", "reply_rate", "by_source"):
        assert k in r
    assert r["learning"]["active"] is False and r["resolved"] == 0
    seed(db, 30)
    r = client.get("/stats").json()
    assert r["resolved"] == 30
    assert r["median_reply_hours"] == pytest.approx(5.0, abs=0.1)
    assert {x["label"] for x in r["by_days_after_posting"]} >= {"same day", "7+ days"}
    assert sum(x["n"] for x in r["by_rent_band"]) == 30
    assert r["learning"]["active"] is True and r["learning"]["factors"]
    assert r["learning"]["blend_weight"] > 0


def test_scoring_path_never_trains_or_scans_outcomes(client, db, monkeypatch):
    """Training happens only in refresh() and /stats; scoring just reads the cached model."""
    seed(db, 30)
    learning.refresh()
    assert learning.current_model()["n"] == 30

    def boom(*a, **k):
        raise AssertionError("scoring path must not train or scan outcomes")
    monkeypatch.setattr(learning, "train", boom)
    monkeypatch.setattr(learning, "collect", boom)
    monkeypatch.setattr(learning, "load_model", boom)   # cached: no DB read either
    from backend.scoring import score_detail
    for l in db.get_all_listings(include_gone=True):
        score_detail(l, {"budget_chf": {"max": 1200}})
    assert client.get("/listings").status_code == 200


def test_current_model_reads_db_once(db, monkeypatch):
    seed(db, 30)
    learning.train(now=NOW)
    monkeypatch.setattr(learning, "_cache", {})
    calls = []
    real = learning.load_model
    monkeypatch.setattr(learning, "load_model", lambda: calls.append(1) or real())
    for _ in range(50):
        assert learning.current_model()["n"] == 30
    assert calls == [1]


def test_refresh_and_status_change_retrain(client, db):
    seed(db, 25)
    assert learning.current_model() is None            # nothing trained yet
    lid = db.get_all_listings()[0]["id"]
    client.post(f"/listings/{lid}/status", json={"status": "rejected"})   # one outcome fewer
    m = learning.current_model()
    assert m is not None and m["n"] == 24
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET status = 'rejected' WHERE id IN "
                     "(SELECT id FROM listings WHERE status IN ('sent','replied') LIMIT 10)")
    assert learning.refresh() is None and learning.current_model() is None   # below 20 again


def test_stats_trains_when_outcomes_age_in(client, db):
    seed(db, 30)
    assert learning.current_model() is None
    r = client.get("/stats").json()
    assert r["learning"]["active"] and learning.current_model()["n"] == 30


def test_stats_page_served_and_linked(client):
    page = client.get("/stats.html")
    assert page.status_code == 200 and "fetch('/stats'" in page.text
    assert "/stats.html" in client.get("/").text      # the ⋯ menu links to it
