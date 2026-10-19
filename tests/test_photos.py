"""Listing photos: image URLs stored at ingest (Flatfox expand=images, Homegate embedded JSON),
shown only by the detail view, never refetched just for the new column. No network."""
import json
from pathlib import Path

from backend.ingestion import homegate_adapter as hg
from backend.ingestion.base import MAX_PHOTOS, photo_list
from backend.ingestion.flatfox_adapter import FlatfoxAdapter
from tests.conftest import FIXTURES, make_ad
from tests.test_incremental import _detail, _pin, _run, flatfox  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parent.parent


def _ff_image(pk: int, name: str) -> dict:
    """One entry of a Flatfox detail response with ?expand=images (shape as live, 2026-10-19)."""
    return {"pk": pk, "caption": "Zimmer", "ordering": 1, "width": 800, "height": 600,
            "url": f"/media/ff/2026/10/{name}.jpg",
            "url_thumb_m": f"/thumb/ff/2026/10/{name}.jpg?alias=thumb_m&signature=abc",
            "url_listing_search": f"/thumb/ff/2026/10/{name}.jpg?alias=listing_search&signature=def"}


# ------------------------------------------------------------------ parsing
def test_photo_list_shapes():
    assert photo_list(None) is None and photo_list({"a": 1}) is None
    assert photo_list([]) == []
    # bare Flatfox image IDs (images not expanded): unknown, not "no photos"
    assert photo_list([50838003, 50838004]) is None
    got = photo_list([_ff_image(1, "a")], "https://flatfox.ch")
    assert got == [{"t": "https://flatfox.ch/thumb/ff/2026/10/a.jpg?alias=thumb_m&signature=abc",
                    "f": "https://flatfox.ch/media/ff/2026/10/a.jpg"}]
    # plain URLs: thumb = full; protocol-relative is upgraded; http and junk are dropped
    assert photo_list(["https://x.test/1.jpg", "//x.test/2.jpg", "http://x.test/3.jpg", "", 7]) == [
        {"t": "https://x.test/1.jpg", "f": "https://x.test/1.jpg"},
        {"t": "https://x.test/2.jpg", "f": "https://x.test/2.jpg"}]
    assert photo_list(["javascript:alert(1)", "/rel.jpg"]) is None    # nothing usable, no base
    assert len(photo_list([f"https://x.test/{i}.jpg" for i in range(30)])) == MAX_PHOTOS


def test_flatfox_to_ad_stores_urls_and_keeps_full_photo_count():
    d = _detail(7)
    d["images"] = [_ff_image(100 + i, f"img{i}") for i in range(15)]
    ad = FlatfoxAdapter()._to_ad(d)
    assert ad["photo_count"] == 15                       # trust input unchanged: the full count
    assert len(ad["photos"]) == MAX_PHOTOS
    assert ad["photos"][0]["f"] == "https://flatfox.ch/media/ff/2026/10/img0.jpg"
    assert ad["photos"][0]["t"].startswith("https://flatfox.ch/thumb/ff/2026/10/img0.jpg?alias=thumb_m")
    # the saved fixtures have bare IDs (older responses): count as before, URLs unknown
    raw = json.loads((FIXTURES / "flatfox_detail.json").read_text())
    for l in raw if isinstance(raw, list) else [raw]:
        a = FlatfoxAdapter()._to_ad(l)
        if a and isinstance(l.get("images"), list) and l["images"]:
            assert a["photo_count"] == len(l["images"]) and a["photos"] is None


def test_flatfox_detail_request_expands_images(flatfox, db):  # noqa: F811
    fake = flatfox([_pin(1)], {1: {**_detail(1), "images": [_ff_image(5, "x")]}})
    calls = []
    orig = fake.get

    def get(url, params=None):
        if isinstance(params, list) and any(k == "pk" for k, _ in params):
            calls.append(params)
        return orig(url, params)
    fake.get = get
    _run()
    assert calls and ("expand", "images") in calls[0]
    row = db.get_listing(db.get_all_listings(True)[0]["id"])
    assert row["photo_count"] == 1
    assert row["photos"] == [{"t": "https://flatfox.ch/thumb/ff/2026/10/x.jpg?alias=thumb_m&signature=abc",
                              "f": "https://flatfox.ch/media/ff/2026/10/x.jpg"}]


def test_homegate_photos_from_embedded_state():
    state = hg.HomegateAdapter._parse_state((FIXTURES / "homegate_state.html").read_text())
    card = {}
    hg.HomegateAdapter._merge_state(card, state["4002086198"])
    ph = card["photos"]
    assert ph and len(ph) <= MAX_PHOTOS
    assert all(p["f"].startswith("https://media2.homegate.ch/listings/") and p["t"] == p["f"] for p in ph)
    assert ph[0]["f"].endswith("92c334cb2a581eeda57f107e6b2d3fd3.jpg")
    assert hg._photos({"localization": {}}) is None and hg._photos({}) is None


# ------------------------------------------------------------------ storage + API
def _photos_of(db, external_id="t1"):
    lid = next(r["id"] for r in db.get_all_listings(True) if r["external_id"] == external_id)
    return db.get_listing(lid)["photos"]


def test_db_roundtrip_and_unknown_keeps_old(db):
    ph = [{"t": "https://c.test/1s.jpg", "f": "https://c.test/1.jpg"}]
    db.upsert_listing(make_ad(photos=ph, photo_count=1))
    assert _photos_of(db) == ph
    # bulk reads (list payloads, trust, notify, group) never carry or decode the URLs
    assert all("photos" not in r for inc in (True, False) for r in db.get_all_listings(inc))
    db.upsert_listing(make_ad(photo_count=1))                     # source sent no list: keep
    assert _photos_of(db) == ph
    db.upsert_listing(make_ad(photos=[], photo_count=0))          # source says none now
    assert _photos_of(db) == []
    db.upsert_listing(make_ad(external_id="t2", photos=[{"f": "https://c.test/x.jpg"}] * 20))
    assert len(_photos_of(db, "t2")) == MAX_PHOTOS


def test_photos_only_change_keeps_trust_cache(db):
    db.upsert_listing(make_ad(photo_count=2))
    before = db.trust_generation()
    db.upsert_listing(make_ad(photo_count=2, photos=[{"t": "https://c.test/a", "f": "https://c.test/a"}]))
    assert db.trust_generation() == before                        # not a trust input


def test_adding_the_column_does_not_force_a_full_refresh(db):
    with db.get_conn() as c:
        c.execute("INSERT INTO source_state (source, key, value) VALUES ('flatfox', 'last_full_refresh', 'x')")
        c.execute("ALTER TABLE listings DROP COLUMN photos")      # a DB from before this column
    db.init_db()
    with db.get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(listings)")}
        kept = c.execute("SELECT value FROM source_state WHERE key = 'last_full_refresh'").fetchone()
    assert "photos" in cols and kept[0] == "x"


def test_detail_endpoint_has_photos_list_does_not(client, db):
    ph = [{"t": "https://c.test/1s.jpg", "f": "https://c.test/1.jpg"}]
    db.upsert_listing(make_ad(photos=ph))
    db.upsert_listing(make_ad(external_id="ap1", kind="apartment", rooms=4.5, rent_chf=3300, photos=ph,
                              raw_text="Schöne 4.5-Zimmer-Wohnung, WG-tauglich."))
    rows = client.get("/listings").json()
    assert rows and not any("photos" in r for r in rows)
    group = client.get("/group/listings?include_dealbreakers=1").json()["listings"]
    assert group and not any("photos" in r for r in group)
    for r in rows:
        assert client.get(f"/listings/{r['id']}").json()["photos"] == ph


# ------------------------------------------------------------------ frontend + service worker
def test_frontend_photo_markup():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    i = html.index("function photoStrip(")
    strip = html[i:html.index("\n}", i)]
    for attr in ('loading="lazy"', 'decoding="async"', 'referrerpolicy="no-referrer"', "Photo ${i + 1} of ${n}"):
        assert attr in strip, attr
    i = html.index("function cardHtml(")
    card = html[i:html.index("\n}", i)]
    assert "photo" not in card.lower()                            # list cards stay light
    assert 'id="photo-viewer" role="dialog" aria-modal="true"' in html


def test_service_worker_never_caches_cross_origin_images():
    sw = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")
    # the only cross-origin host the SW touches is unpkg (Leaflet); everything else returns
    # before any respondWith/cache.put, so CDN photos go straight to the network
    early = sw.index("if (url.origin !== self.location.origin) return;")
    assert sw.count("c.put(") == 2 and sw.index("c.put(") < early        # unpkg branch only...
    assert "url.hostname === 'unpkg.com'" in sw[:early]
    assert sw.index("c.put(", sw.index("c.put(") + 1) > early         # ...and the same-origin shell
