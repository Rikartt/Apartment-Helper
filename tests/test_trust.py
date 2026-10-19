"""Trust score: phrase signals, median price, duplicate fingerprints, API fields, calibration.
Fully offline; no Anthropic client is ever built (conftest forbids it)."""
import json

import httpx
import pytest

from backend import notify, trust
from tests.conftest import FIXTURES, make_ad

# ---------------------------------------------------------------- text signals
SCAM_SNIPPETS = [
    ("Bitte überweisen Sie die Kaution vor der Besichtigung, dann erhalten Sie die Adresse.", "payment or deposit"),
    ("Die Kaution muss vorab auf mein Konto überwiesen werden.", "payment or deposit"),
    ("To reserve the room please pay a deposit of 800 CHF before the viewing.", "payment or deposit"),
    ("Reservierungsgebühr von 300 CHF fällig.", "payment or deposit"),
    ("Pour la visite, payez l'acompte avant la visite par virement.", "payment or deposit"),
    ("Ich bin im Ausland, die Schlüssel werden per Post zugeschickt.", "Untraceable"),
    ("I live abroad. The keys will be sent by post after you transfer first.", "Untraceable"),
    ("Payment only by Western Union please.", "Untraceable"),
    ("Das Zimmer läuft über Airbnb, Sie bezahlen zuerst.", "Untraceable"),
    ("Contact me only on WhatsApp.", "WhatsApp"),
    ("Bitte nur per E-Mail an mein Gmail schreiben.", "WhatsApp"),
    ("Sehr viele Interessenten, bitte heute noch entscheiden!", "Pressure"),
    ("Many people are interested, so decide today.", "Pressure"),
    ("Beaucoup d'intéressés, décidez aujourd'hui.", "Pressure"),
    ("Molti interessati, decidi oggi.", "Pressure"),
]

LEGIT_SNIPPETS = [
    "Die Kaution beträgt zwei Monatsmieten und wird bei Vertragsunterzeichnung auf ein Mietkautionskonto einbezahlt.",
    "Die Kaution ist vor dem Einzug fällig.",
    "Die Miete ist jeweils im Voraus zum Monatsersten zahlbar.",
    "Keine Kaution vor der Besichtigung, natürlich erst nach dem Vertrag.",
    "We never ask for any deposit before the viewing.",
    "Ich bin im Auslandssemester, deshalb Zwischenmiete bis Juni.",  # abroad alone is only mild
    "Kontakt via Mail oder WhatsApp, wie du magst.",
    "Es haben sich schon einige gemeldet, wir melden uns bei allen.",
    "Besichtigung am Donnerstag um 18 Uhr, melde dich bitte vorher.",
    "Deposit is held in a blocked account in your name.",
]


@pytest.mark.parametrize("text,reason", SCAM_SNIPPETS)
def test_scam_phrases_detected(text, reason):
    sig = trust.text_signals(text)
    assert sig, text
    assert any(reason.lower() in r.lower() for _, r in sig)


@pytest.mark.parametrize("text", LEGIT_SNIPPETS)
def test_legit_phrases_not_strong(text):
    # a legit snippet may trip at most a mild signal, never a strong (>= 45) one
    assert all(w < 45 for w, _ in trust.text_signals(text)), text


def test_negation_scopes_to_sentence():
    # the negation in the previous sentence must not hide a later demand
    sig = trust.text_signals("Ich habe keine Zeit für Telefon. Bitte Kaution vorab überweisen.")
    assert any(w >= 55 for w, _ in sig)


# ---------------------------------------------------------------- median price
def _room(i, rent, **kw):
    base = dict(id=i, external_id=f"r{i}", rent_chf=rent, address=f"Teststrasse {i}, 8004 Zürich",
                lat=47.37 + i * 0.0005, lng=8.52, raw_text=f"unique text number {i} " * 3)
    base.update(kw)
    return make_ad(**base)


def test_far_below_median_penalised():
    rows = [_room(i, 900 + i * 10) for i in range(1, 9)] + [_room(20, 380)]
    d = trust.trust_detail(rows[-1], trust.TrustContext(rows))
    assert d["score"] <= 65 and d["level"] == "medium"
    assert any("below the median" in r for r in d["reasons"])


def test_normal_and_slightly_cheap_ok():
    rows = [_room(i, 900 + i * 10) for i in range(1, 9)] + [_room(20, 700)]
    d = trust.trust_detail(rows[-1], trust.TrustContext(rows))
    assert d["level"] == "high"


def test_median_needs_enough_comparables_and_same_kind():
    rows = [_room(i, 1000) for i in range(1, 4)] + [_room(20, 300)]
    assert trust.TrustContext(rows).median_rent(rows[-1])[0] is None
    rows = [_room(i, 1000, kind="studio") for i in range(1, 9)] + [_room(20, 300)]
    assert trust.TrustContext(rows).median_rent(rows[-1])[0] is None   # other kind: not comparable


def test_comparables_use_radius_or_plz():
    far = _room(30, 1000, lat=47.50, lng=8.80, address="Weitweg 1, 8900 Fern")
    near = _room(31, 1000)
    me = _room(32, 900)
    ctx = trust.TrustContext([far, near, me])
    ids = {o["id"] for o in ctx.comparables(me)}
    assert 31 in ids and 30 not in ids
    same_plz = _room(33, 1000, lat=47.50, lng=8.80)    # far away, but same PLZ 8004
    assert 33 in {o["id"] for o in trust.TrustContext([same_plz, me]).comparables(me)}


def test_implausible_price_and_median_not_double_counted():
    rows = [_room(i, 900) for i in range(1, 9)]
    bad = _room(20, 100)   # price_implausible() fires (< 200) and the median rule too
    d = trust.trust_detail(bad, trust.TrustContext(rows + [bad]))
    price_reasons = [r for r in d["reasons"] if "Price looks wrong" in r or "below the median" in r]
    assert len(price_reasons) == 1
    assert d["score"] == 100 - 35


# ---------------------------------------------------------------- duplicates
LONG = ("Gemütliches helles Zimmer nahe ETH und Universität in einer ruhigen Wohnung mit Parkettboden "
        "und grossem Fenster. Die Wohnung ist frisch renoviert, hat eine neue Küche und ein modernes Bad. "
        "Das Zimmer ist komplett möbliert und sofort bezugsbereit für Studierende oder Berufstätige.")


def test_fingerprint_near_identical_and_unrelated():
    a = trust.fingerprint(LONG)
    b = trust.fingerprint(LONG.replace("helles", "sonniges").upper() + " Bitte melden.")
    c = trust.fingerprint("Wir sind eine WG von drei Studenten und kochen gerne zusammen. " * 3 +
                          "Am Wochenende gehen wir oft wandern oder spielen Brettspiele im Wohnzimmer.")
    assert trust.similarity(a, b) >= 0.8
    assert trust.similarity(a, c) < 0.3
    assert trust.fingerprint("Zu kurz.") is None


def test_duplicate_different_address_and_price():
    a = make_ad(id=1, external_id="a", source="flatfox", address="Rämistrasse 71, 8006 Zürich",
                rent_chf=1350, raw_text=LONG)
    b = make_ad(id=2, external_id="b", source="homegate", address="Kanzleistrasse 5, 8004 Zürich",
                rent_chf=720, raw_text=LONG + " Danke.")
    ctx = trust.TrustContext([a, b])
    d = trust.trust_detail(a, ctx)
    assert d["score"] == 55
    assert any("different address and a very different price" in r for r in d["reasons"])


def test_cross_post_same_place_same_price_is_fine():
    a = make_ad(id=1, external_id="a", source="flatfox", address="Rämistrasse 71, 8006 Zürich",
                rent_chf=1350, raw_text=LONG)
    b = make_ad(id=2, external_id="b", source="homegate", address="Rämistrasse 71, 8006 Zürich",
                rent_chf=1350, raw_text=LONG)
    d = trust.trust_detail(a, trust.TrustContext([a, b]))
    assert not any("Same description" in r for r in d["reasons"])


def test_duplicate_different_address_same_price_is_mild():
    a = make_ad(id=1, external_id="a", address="Rämistrasse 71, 8006 Zürich", rent_chf=1000, raw_text=LONG)
    b = make_ad(id=2, external_id="b", address="Kanzleistrasse 5, 8004 Zürich", rent_chf=1000, raw_text=LONG)
    d = trust.trust_detail(a, trust.TrustContext([a, b]))
    assert d["score"] == 75 and d["level"] == "high"


# ---------------------------------------------------------------- mild signals / red flags
def test_mild_signals_never_reach_low():
    l = make_ad(id=1, address="Zürich", photo_count=0, extracted={"poster_type": "agency"},
                raw_text="Zimmer frei.")
    d = trust.trust_detail(l)
    assert d["score"] == 100 - 8 - 10 - 3 and d["level"] == "high"
    assert {"No street or house number in the ad", "No photos"} <= set(d["reasons"])


def test_house_number_detection():
    ok = lambda a, **kw: trust.has_house_number(make_ad(address=a, **kw))
    assert ok("Birmensdorferstrasse 112, 8003 Zürich")
    assert ok("Hardstr. 5a, Zürich")
    assert not ok("Zürich Wiedikon")
    assert not ok("Zürich 4")
    assert not ok("8003 Zürich")
    assert ok("Zürich", raw_text="Das Zimmer an der Langstrasse 120 ist hell.")


def test_photo_count_only_when_reported():
    assert "No photos" not in trust.trust_detail(make_ad(id=1, address="Teststrasse 1"))["reasons"]
    assert "No photos" in trust.trust_detail(make_ad(id=1, address="Teststrasse 1", photo_count=0))["reasons"]


def test_red_flags_from_extraction_count():
    l = make_ad(id=1, address="Teststrasse 1", extracted={"red_flags": ["asks for deposit before viewing"]})
    d = trust.trust_detail(l)
    assert d["score"] == 88 and "Flagged by ad analysis" in d["reasons"][0]
    l["extracted"]["red_flags"] = ["a", "b", "c", "d"]
    assert trust.trust_detail(l)["score"] == 100 - 24   # at most two flags count


# ---------------------------------------------------------------- calibration
def _fixture_ads():
    ads = json.loads((FIXTURES / "trust_ads.json").read_text())
    for i, a in enumerate(ads, 1):
        a["id"] = i
        a.setdefault("rooms", None)
    trust.annotate(ads)
    return ads


def test_calibration_on_synthetic_zurich_ads():
    ads = _fixture_ads()
    legit = [a for a in ads if a["label"] == "legit"]
    scam = [a for a in ads if a["label"] == "scam"]
    assert len(ads) >= 30
    # conservative: no real listing may land in "low"
    assert [a["title"] for a in legit if a["trust_level"] == "low"] == []
    assert all(a["trust_score"] >= 75 for a in legit)
    # every scam is at least medium-or-worse; the explicit ones are low
    assert all(a["trust_level"] != "high" for a in scam)
    low = [a for a in scam if a["trust_level"] == "low"]
    assert len(low) >= 6
    print(f"legit={len(legit)} scam={len(scam)} low-flagged scams={len(low)} "
          f"false-low={sum(a['trust_level'] == 'low' for a in legit)}")


# ---------------------------------------------------------------- API + alerts
SCAM_TEXT = ("Ich bin im Ausland. Bitte Kaution vorab überweisen, die Schlüssel kommen per Post. "
             "Western Union ist ok. Nur über WhatsApp melden.")


def test_listings_api_has_trust_fields(client, db):
    db.upsert_listing(make_ad(external_id="ok", address="Teststrasse 1, 8004 Zürich"))
    db.upsert_listing(make_ad(external_id="bad", address="Teststrasse 2, 8004 Zürich", raw_text=SCAM_TEXT))
    rows = {r["external_id"]: r for r in client.get("/listings").json()}
    assert rows["ok"]["trust_level"] == "high" and rows["ok"]["trust_score"] == 100
    assert rows["ok"]["trust_reasons"] == []
    assert rows["bad"]["trust_level"] == "low" and rows["bad"]["trust_reasons"]
    assert "raw_text" not in rows["bad"]            # still omitted by default
    one = client.get(f"/listings/{rows['bad']['id']}").json()
    assert one["trust_level"] == "low" and one["raw_text"]
    assert one["trust_reasons"] == rows["bad"]["trust_reasons"]
    # status updates return the same fields
    st = client.post(f"/listings/{rows['bad']['id']}/status", json={"status": "sent"}).json()
    assert st["trust_level"] == "low"


def test_group_listings_api_has_trust_fields(client, db):
    db.upsert_listing(make_ad(external_id="ap1", kind="apartment", rooms=4.5, rent_chf=2400,
                              address="Teststrasse 1, 8004 Zürich", raw_text=SCAM_TEXT))
    client.put("/profile", json={"group": {"size": 3, "budget_per_person_chf": 1000, "members": []}})
    body = client.get("/group/listings?include_dealbreakers=1").json()
    assert body["listings"], body
    l = body["listings"][0]
    assert l["trust_level"] == "low" and l["trust_reasons"] and "raw_text" not in l


def test_phone_alerts_skip_low_trust(db, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t-abc")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    monkeypatch.setenv("WGP_NOTIFY_QUIET", "")
    monkeypatch.setattr(notify, "load_profile",
                        lambda: {"budget_chf": {"max": 1000}, "languages": [], "traits": [], "wants": []})
    calls = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: calls.append(kw) or
                        httpx.Response(200, request=httpx.Request("POST", url)))
    db.upsert_listing(make_ad(external_id="ok", url="https://ex.test/ok", transit_zentrum_min=10))
    db.upsert_listing(make_ad(external_id="bad", url="https://ex.test/bad", transit_zentrum_min=10,
                              raw_text=SCAM_TEXT))
    cands = notify.pending_candidates()
    assert [c["url"] for c in cands] == ["https://ex.test/ok"]
    assert notify.notify_new()["sent"] == 1
    assert b"ex.test/bad" not in calls[0]["content"]


def test_ingest_warms_trust_cache(db, monkeypatch):
    """After an ingest the trust scores are already computed, so /listings hits the cache."""
    import backend.main as m
    db.upsert_listing(make_ad(external_id="w1"))
    monkeypatch.setattr(m, "run_ingest", lambda names: {"total": 1, "new": 0, "gone": 0,
                        "sources": {names[0]: {"ok": True, "fetched": 1, "new": 0}}})
    monkeypatch.setattr(trust, "_cache", {"entry": None})
    m._tracked_ingest(["mock"])
    entry = trust._cache["entry"]
    assert entry is not None and len(entry[1]) == 1
    calls = []
    monkeypatch.setattr(trust, "TrustContext", lambda *a: calls.append(a))
    trust.annotate(db.get_all_listings(True))
    assert calls == []   # cache hit, no recompute
