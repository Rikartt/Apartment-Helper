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


def _count_trains(monkeypatch):
    calls = []
    real = learning.train
    monkeypatch.setattr(learning, "train", lambda *a, **k: calls.append(1) or real(*a, **k))
    return calls


def test_stats_one_class_does_not_retrain_every_call(client, db, monkeypatch):
    """Only no-replies so far: no model is possible, and /stats must not re-attempt the
    fit on every call. It tries again only once the outcomes change."""
    seed(db, 25, replies_if_fresh=False)
    calls = _count_trains(monkeypatch)
    for _ in range(3):
        r = client.get("/stats").json()
        assert r["resolved"] == 25 and r["learning"]["active"] is False
    assert calls == [1]
    # same count, other class: a no-reply turns into a reply without going through refresh()
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET status = 'replied' WHERE id = "
                     "(SELECT id FROM listings WHERE status = 'sent' LIMIT 1)")
    r = client.get("/stats").json()
    assert len(calls) == 2 and r["learning"]["active"] is True
    assert learning.current_model()["n"] == 25
    client.get("/stats")
    assert len(calls) == 2                       # trained model is cached as before


def test_stats_one_class_retrains_when_an_outcome_is_added(client, db, monkeypatch):
    seed(db, 25, replies_if_fresh=False)
    calls = _count_trains(monkeypatch)
    client.get("/stats"), client.get("/stats")
    assert calls == [1]
    sent = NOW - timedelta(days=12)
    db.upsert_listing(dict(source="flatfox", external_id="new", title="t", address="Zürich",
                           lat=47.37, lng=8.54, rent_chf=900, available_from="2026-11-01",
                           raw_text="x", url="https://e.test/new",
                           listed_at=_fmt(sent - timedelta(days=1))))
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET status = 'replied', status_updated_at = ? "
                     "WHERE external_id = 'new'", (_fmt(sent + timedelta(hours=3)),))
    r = client.get("/stats").json()
    assert len(calls) == 2 and r["learning"]["active"] is True
    assert learning.current_model()["n"] == 26


@pytest.mark.parametrize("n", [0, 5])
def test_sync_without_enough_outcomes_never_trains(db, monkeypatch, n):
    """Zero outcomes and too few outcomes: repeated sync() calls never fit."""
    if n:
        seed(db, n)
    calls = _count_trains(monkeypatch)
    for _ in range(3):
        assert learning.sync(NOW) is None
    assert calls == []


def test_sync_clears_stale_model_once_when_outcomes_drop(db, monkeypatch):
    seed(db, 25)
    assert learning.train(now=NOW)["n"] == 25
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET status = 'rejected' WHERE id IN "
                     "(SELECT id FROM listings WHERE status IN ('sent','replied') LIMIT 10)")
    calls = _count_trains(monkeypatch)
    for _ in range(3):
        assert learning.sync(NOW) is None
    assert calls == [1] and learning.current_model() is None


def test_sync_one_class_direct_calls_attempt_once(db, monkeypatch):
    seed(db, 25, replies_if_fresh=False)
    calls = _count_trains(monkeypatch)
    for _ in range(4):
        assert learning.sync(NOW) is None
    assert calls == [1]


def test_sync_retries_after_a_failed_fit(db, monkeypatch):
    """A fit that crashes (e.g. 'database is locked' while storing the weights) is not a
    finished attempt: the next sync() must try again instead of serving no model until
    the outcomes happen to change."""
    import sqlite3
    seed(db, 25)
    real_store, fails = learning._store, []

    def flaky_store(model):
        if not fails:
            fails.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real_store(model)
    monkeypatch.setattr(learning, "_store", flaky_store)
    assert learning.sync(NOW) is None            # the failed attempt
    m = learning.sync(NOW)
    assert m is not None and m["n"] == 25


# ---- background retrain (status buttons must not wait for a fit) ---------------------

@pytest.fixture
def background(monkeypatch):
    """Real background worker (conftest turns it off for every other test), no quiet period."""
    monkeypatch.setenv("WGP_LEARNING_BACKGROUND", "1")
    monkeypatch.setattr(learning, "QUIET_S", 0.0)
    yield
    assert learning.wait_idle(10), "background retrain did not finish"


def _blocking_refresh(monkeypatch):
    """refresh() replaced by one that records its call and waits for `release`."""
    import threading
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(now=None):
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(10)
    monkeypatch.setattr(learning, "refresh", slow)
    return started, release, calls


@pytest.mark.parametrize("call", ["status", "sent"])
def test_endpoints_return_without_waiting_for_training(client, db, background, monkeypatch, call):
    import time
    seed(db, 25)
    lid = db.get_all_listings()[0]["id"]
    started, release, calls = _blocking_refresh(monkeypatch)
    t0 = time.perf_counter()
    if call == "status":
        r = client.post(f"/listings/{lid}/status", json={"status": "replied"})
    else:
        r = client.post(f"/listings/{lid}/sent", json={"text": "Hallo", "kind": "followup"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert started.wait(5)                  # the retrain did start ...
    assert not release.is_set() and elapsed < 2   # ... but the reply did not wait for it
    release.set()
    assert learning.wait_idle(5)
    assert calls == ["learning-refresh"]    # once, on the worker thread


def test_rapid_taps_coalesce_into_one_pending_retrain(db, background, monkeypatch):
    started, release, calls = _blocking_refresh(monkeypatch)
    assert learning.schedule_refresh() is True
    assert started.wait(5)                  # first retrain running (blocked)
    queued = [learning.schedule_refresh() for _ in range(10)]
    assert queued == [True] + [False] * 9   # one waits behind it, the rest fold into that one
    assert not learning.wait_idle(0.05)
    release.set()
    assert learning.wait_idle(5)
    assert len(calls) == 2                  # 11 taps, 2 fits; the last starts after the last tap


def test_retrain_waits_until_taps_go_quiet(db, background, monkeypatch):
    """A burst of taps is not slowed by a fit in between: one retrain, after the last tap."""
    import time
    monkeypatch.setattr(learning, "QUIET_S", 0.3)
    started, release, calls = _blocking_refresh(monkeypatch)
    release.set()
    last = None
    for _ in range(4):
        learning.schedule_refresh()
        last = time.monotonic()
        time.sleep(0.1)
        assert not started.is_set()         # still inside the quiet period of the last tap
    assert started.wait(5)
    assert time.monotonic() - last >= 0.3
    assert learning.wait_idle(5, flush=False) and len(calls) == 1


def test_background_retrain_updates_model_and_stats(client, db, background):
    seed(db, 25)
    client.get("/stats")
    assert learning.current_model()["n"] == 25
    lid = next(l["id"] for l in db.get_all_listings() if l["status"] in ("sent", "replied"))
    assert client.post(f"/listings/{lid}/status", json={"status": "rejected"}).status_code == 200
    # no wait: /stats counts are read live, and sync() waits for (or does) the retrain
    r = client.get("/stats").json()
    assert r["resolved"] == 24 and r["learning"]["active"]
    assert learning.current_model()["n"] == 24
    assert learning.wait_idle(5)
    assert learning.current_model()["n"] == 24 and learning.load_model()["n"] == 24


def test_background_retrain_stays_on_the_db_it_was_asked_for(db, background, monkeypatch, tmp_path):
    """The worker pins the DB file of the request, even if DB_PATH changes meanwhile."""
    seed(db, 25)
    path = db.DB_PATH
    real = learning.refresh
    started, release, _ = _blocking_refresh(monkeypatch)
    blocked = learning.refresh
    monkeypatch.setattr(learning, "refresh", lambda now=None: (blocked(), real())[1])
    learning.schedule_refresh()
    assert started.wait(5)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "other.db")
    release.set()
    assert learning.wait_idle(5)
    assert learning._cache[str(path)]["n"] == 25 and str(tmp_path / "other.db") not in learning._cache
    monkeypatch.setattr(db, "DB_PATH", path)
    assert learning.load_model()["n"] == 25


def test_inline_mode_retrains_before_the_reply(client, db, monkeypatch):
    """WGP_LEARNING_BACKGROUND=0 (the test default): schedule_refresh() is refresh()."""
    import threading
    calls = []
    monkeypatch.setattr(learning, "refresh", lambda now=None: calls.append(threading.current_thread().name))
    assert learning.schedule_refresh() is False
    assert calls == [threading.current_thread().name]
    seed(db, 25)
    lid = db.get_all_listings()[0]["id"]
    client.post(f"/listings/{lid}/status", json={"status": "rejected"})
    assert len(calls) == 2 and not learning._pending and not learning._running
