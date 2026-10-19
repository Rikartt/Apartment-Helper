"""Regression tests for bugs found when the six feature branches were combined on main."""
from tests.test_group import G, apt

SCAM = ("Schönes Zimmer. Ich bin zurzeit im Ausland. Bitte die Kaution vor der Besichtigung "
        "per Western Union überweisen, die Schlüssel werden per Post geschickt.")


def test_group_listings_text_checks_see_the_ad_body(client, db, ad_factory):
    """/group/listings stripped raw_text before scoring, so "keine WG" in the ad body (not the
    title) never became a dealbreaker there, while phone alerts (raw text) did see it."""
    db.upsert_listing(apt(ad_factory, external_id="nowg",
                          raw_text="Schöne Wohnung, ruhige Lage. Keine WG erlaubt, nur an Familien."))
    db.upsert_listing(apt(ad_factory, external_id="ok", raw_text="Schöne Wohnung, ruhige Lage."))
    client.put("/profile", json={"group": G})
    body = client.get("/group/listings").json()
    assert {l["external_id"] for l in body["listings"]} == {"ok"}
    full = client.get("/group/listings?include_dealbreakers=1").json()["listings"]
    bad = next(l for l in full if l["external_id"] == "nowg")
    assert "ad excludes shared flats / students" in bad["group_dealbreakers"]
    assert all("raw_text" not in l for l in full)   # still not shipped to the browser


def test_group_listings_carry_followup_fields(client, db, ad_factory, monkeypatch):
    """The group-mode list must carry followup_due / days_since_sent like /listings does,
    or the Follow-ups filter and the "Follow up?" chip stay empty in group mode."""
    db.upsert_listing(apt(ad_factory))
    lid = db.get_all_listings()[0]["id"]
    client.put("/profile", json={"group": G})
    db.add_sent_message(lid, "Guten Tag ...", "first")
    with db.get_conn() as c:   # sent 5 days ago
        c.execute("UPDATE sent_messages SET sent_at = datetime('now', '-5 days')")
        c.execute("UPDATE listings SET status_updated_at = datetime('now', '-5 days')")
    solo = client.get(f"/listings/{lid}").json()
    assert solo["followup_due"] is True
    g = client.get("/group/listings").json()["listings"][0]
    assert g["followup_due"] is True and g["days_since_sent"] >= 4.9 and g["sent_at"]


def test_best_skips_low_trust_listings(client, db, ad_factory):
    """Top 5 must not recommend a likely scam the main list hides by default."""
    db.upsert_listing(ad_factory(external_id="scam", rent_chf=700, raw_text=SCAM,
                                 address="Langstrasse 5, 8004 Zürich",
                                 transit_zentrum_min=8, transit_hoenggerberg_min=15))
    db.upsert_listing(ad_factory(external_id="fine", rent_chf=800, raw_text="Nettes Zimmer in WG.",
                                 address="Langstrasse 7, 8004 Zürich",
                                 transit_zentrum_min=20, transit_hoenggerberg_min=30))
    rows = {l["external_id"]: l for l in client.get("/listings").json()}
    assert rows["scam"]["trust_level"] == "low"
    best = client.get("/best").json()
    assert [l["external_id"] for l in best] == ["fine"]
