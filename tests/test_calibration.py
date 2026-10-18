"""Score spread and implausible-price handling."""
from datetime import date

from backend.scoring import score_detail, price_implausible, squash
from backend import group as g

TODAY = date(2026, 10, 18)
PROFILE = {"budget_chf": {"max": 1000}, "languages": [{"lang": "German", "level": "native"}],
           "traits": ["tidy", "friendly"], "wants": ["social"], "status": "ETH student"}


def room(**kw):
    d = dict(rent_chf=700, kind="wg_room", available_from="flexible",
             transit_zentrum_min=10, transit_hoenggerberg_min=15,
             listed_at="2026-10-17T10:00:00+02:00", raw_text="x" * 20)
    d.update(kw)
    return d


def test_squash_is_monotonic_and_bounded():
    vals = [squash(r) for r in range(0, 200, 2)]
    assert vals == sorted(vals)
    assert min(vals) == 30 and max(vals) <= 98


def test_best_listings_do_not_saturate():
    ext = {"vibe": ["friendly social tidy"], "preferences": ["social"], "poster_type": "private",
           "language": "de", "duration": "permanent", "red_flags": []}
    a = score_detail(room(extracted=ext, rent_chf=600), PROFILE, TODAY)
    b = score_detail(room(extracted=ext, rent_chf=700, transit_zentrum_min=14), PROFILE, TODAY)
    assert a["fit"] < 100 and b["fit"] < 100
    assert a["fit_precise"] > b["fit_precise"]
    bare = score_detail(room(rent_chf=950, transit_zentrum_min=40, transit_hoenggerberg_min=50,
                             listed_at=None), PROFILE, TODAY)
    assert 30 <= bare["fit"] < a["fit"] - 15


def test_implausible_price_is_dealbreaker_not_deleted():
    assert price_implausible(room(rent_chf=150))
    assert price_implausible(room(rent_chf=0)) is None
    assert price_implausible(room(rent_chf=450)) is None
    d = score_detail(room(rent_chf=150), PROFILE, TODAY)
    assert d["fit"] == 0 and any("Price looks wrong" in x for x in d["dealbreakers"])


def test_apartment_price_rules():
    assert price_implausible(dict(kind="apartment", rent_chf=500, rooms=4.5))
    assert price_implausible(dict(kind="apartment", rent_chf=700, rooms=5.5))
    assert price_implausible(dict(kind="apartment", rent_chf=900, rooms=2.5, living_area_m2=120))
    assert price_implausible(dict(kind="apartment", rent_chf=2800, rooms=4.5, living_area_m2=95)) is None


def test_group_flags_cheap_flat():
    grp = {"size": 3, "budget_per_person_chf": 1100, "members": [], "notes": ""}
    l = dict(kind="apartment", rent_chf=500, rooms=4.5, transit_zentrum_min=10,
             transit_hoenggerberg_min=20, raw_text="", title="Wohnung")
    d = g.group_detail(l, grp, TODAY)
    assert any("Price looks wrong" in x for x in d["group_dealbreakers"])
    assert g.group_listings([l], grp) == []
    assert len(g.group_listings([l], grp, include_dealbreakers=True)) == 1
    ok = dict(l, rent_chf=2800)
    assert g.group_detail(ok, grp, TODAY)["group_dealbreakers"] == []
    assert 20 <= g.group_detail(ok, grp, TODAY)["group_fit_score"] <= 98


# ---- drafting: past move-in dates ----
def test_profile_for_draft_past_date_becomes_immediate():
    from backend.drafting import profile_for_draft
    p = profile_for_draft({"move_in_earliest": "2026-09-01", "age": 18}, TODAY)
    assert "move_in_earliest" not in p and "immediately" in p["move_in"]
    assert "2026-09-01" not in str(p)
    p = profile_for_draft({"move_in_earliest": "2026-12-01"}, TODAY)
    assert p["move_in"] == "from 2026-12-01"
    p = profile_for_draft({"age": 18}, TODAY)
    assert "move_in" not in p


def test_solo_and_group_prompts_use_immediate_move_in():
    from backend import drafting
    prof = {"move_in_earliest": "2020-01-01", "group": {"size": 3}}
    listing = {"title": "t", "rent_chf": 2800, "rooms": 4.5, "raw_text": "Wohnung", "extracted": {}}
    grp = {"size": 3, "budget_per_person_chf": 1100, "members": [], "notes": ""}
    _, system, user = g.build_group_prompt(listing, prof, grp)
    assert "2020-01-01" not in user and "available immediately" in user
    assert "never quote a past calendar date" in system or "past calendar date" in system
    assert "past" in drafting.SYSTEM_PROMPT


# ---- review fixes ----
def test_get_conn_context_closes(db):
    import sqlite3
    with db.get_conn() as c:
        c.execute("SELECT 1")
    try:
        c.execute("SELECT 1")
        closed = False
    except sqlite3.ProgrammingError:
        closed = True
    assert closed


def test_malformed_profile_does_not_brick_scoring(client, profile_path, db, ad_factory):
    import json
    db.upsert_listing(ad_factory(rent_chf=700))
    profile_path.write_text(json.dumps({"budget_chf": {"max": "lots"}, "languages": 5, "traits": "x"}))
    r = client.get("/listings")
    assert r.status_code == 200 and r.json()[0]["fit_score"] > 0
    profile_path.write_text(json.dumps({"budget_chf": [1], "languages": ["German", None]}))
    assert client.get("/listings").status_code == 200


def test_best_excludes_apartments_and_kind_filter(client, db, ad_factory):
    db.upsert_listing(ad_factory(external_id="r1", kind="wg_room", rent_chf=700))
    db.upsert_listing(ad_factory(external_id="a1", kind="apartment", rent_chf=900, rooms=3.5))
    ids = {x["external_id"] for x in client.get("/best").json()}
    assert ids == {"r1"}
    all_ids = {x["external_id"] for x in client.get("/listings").json()}
    assert all_ids == {"r1", "a1"}                      # default unchanged
    only = {x["external_id"] for x in client.get("/listings", params={"kind": "apartment"}).json()}
    assert only == {"a1"}


def test_ingest_endpoint_409_when_lock_held_and_400_on_bad_source(client):
    import backend.main as m
    assert client.post("/ingest", params={"sources": "nope"}).status_code == 400
    assert m._ingest_lock.acquire()
    try:
        assert client.post("/ingest", params={"sources": "mock"}).status_code == 409
    finally:
        m._ingest_lock.release()


def test_extract_batch_single_flight(monkeypatch, db):
    from backend import extraction
    assert extraction._batch_lock.acquire()
    try:
        r = extraction.extract_batch(limit=5)
        assert r["error_status"] == 409 and r["extracted"] == 0
    finally:
        extraction._batch_lock.release()


def test_homegate_drops_ads_it_cannot_place(monkeypatch):
    from backend.ingestion import homegate_adapter as hg
    monkeypatch.setattr(hg, "_geocode", lambda a: (None, None))
    card = {"external_id": "1" * 12, "url": "u", "rent_chf": 900, "address": "Nowhere 1",
            "title_raw": "WG Zimmer", "desc": "", "kind": "wg_room"}
    assert hg.HomegateAdapter()._enrich(card) is None


def test_profile_put_is_atomic_file(client, profile_path):
    r = client.put("/profile", json={"budget_chf": {"max": 900}})
    assert r.status_code == 200
    assert not profile_path.with_name(profile_path.name + ".tmp").exists()


def test_concurrent_ingest_writes_and_request_reads(db, ad_factory):
    import threading
    errors = []

    def writer(n):
        try:
            for i in range(60):
                db.upsert_listing(ad_factory(external_id=f"w{n}-{i}"))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(60):
                db.get_all_listings(False)
                db.get_stats()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=writer, args=(n,)) for n in range(3)] + \
         [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(db.get_all_listings(False)) == 180
