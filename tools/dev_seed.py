"""Build a realistic synthetic WGPendler database for development and UI checks.

    python tools/dev_seed.py /tmp/wgp-dev.db                 # ~200 listings
    python tools/dev_seed.py /tmp/wgp-dev.db --outcomes      # plus sent/replied history
    python tools/dev_seed.py /tmp/wgp-dev.db --n 400 --seed 3 --force

What you get (deterministic for a given --seed): WG rooms, studios and whole flats spread
over Zürich neighbourhoods, German and English ad texts, two obvious scam ads (they come
out as low trust), extracted fields on about a third of the listings (as if Extract had
run), commute minutes from the app's own estimator, fit/reply scores from the app's own
scoring, and one finished ingest_run row. With --outcomes some listings are marked sent or
replied with a stored first message from 12 days ago, so the reply stats page, the
"Follow up?" chip and the follow-up draft button all have something to show; this
includes whole flats for group mode.

No network, no Anthropic calls: it only uses backend.db / backend.scoring /
backend.commute against the given path (the real wgpendler.db is never touched).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as dbm  # noqa: E402
from backend import scoring  # noqa: E402
from backend.commute import commute_times  # noqa: E402

# (neighbourhood, PLZ, lat, lng, streets)
AREAS = [
    ("Oberstrass", "8006", 47.3860, 8.5480, ["Universitätstrasse", "Hochstrasse", "Scheuchzerstrasse"]),
    ("Wipkingen", "8037", 47.3930, 8.5270, ["Rosengartenstrasse", "Nordstrasse", "Hönggerstrasse"]),
    ("Höngg", "8049", 47.4030, 8.4980, ["Limmattalstrasse", "Regensdorferstrasse", "Wieslergasse"]),
    ("Oerlikon", "8050", 47.4110, 8.5450, ["Schaffhauserstrasse", "Hofwiesenstrasse", "Binzmühlestrasse"]),
    ("Affoltern", "8046", 47.4210, 8.5100, ["Wehntalerstrasse", "Zehntenhausstrasse", "Wolfswinkel"]),
    ("Langstrasse", "8004", 47.3780, 8.5260, ["Langstrasse", "Badenerstrasse", "Hohlstrasse"]),
    ("Wiedikon", "8003", 47.3700, 8.5180, ["Birmensdorferstrasse", "Zurlindenstrasse", "Weststrasse"]),
    ("Seefeld", "8008", 47.3560, 8.5550, ["Seefeldstrasse", "Kreuzstrasse", "Dufourstrasse"]),
    ("Fluntern", "8044", 47.3800, 8.5640, ["Gloriastrasse", "Zürichbergstrasse", "Hochstrasse"]),
    ("Hottingen", "8032", 47.3690, 8.5600, ["Asylstrasse", "Hottingerstrasse", "Freiestrasse"]),
    ("Altstetten", "8048", 47.3880, 8.4850, ["Badenerstrasse", "Hohlstrasse", "Altstetterstrasse"]),
    ("Schwamendingen", "8051", 47.4050, 8.5720, ["Winterthurerstrasse", "Saatlenstrasse", "Dübendorfstrasse"]),
    ("Kreis 5", "8005", 47.3880, 8.5200, ["Hardturmstrasse", "Josefstrasse", "Limmatstrasse"]),
    ("Enge", "8002", 47.3630, 8.5310, ["Seestrasse", "Bederstrasse", "Tödistrasse"]),
    ("Wollishofen", "8038", 47.3440, 8.5300, ["Albisstrasse", "Kilchbergstrasse", "Mutschellenstrasse"]),
    ("Dübendorf", "8600", 47.3970, 8.6180, ["Bahnhofstrasse", "Wallisellenstrasse", "Usterstrasse"]),
]

# WG ads are composed from sentence pools so no two ads share a description (the trust
# score flags copied descriptions, and real ads are rarely identical).
WG_DE = {
    "intro": ["Wir sind eine entspannte {hh}er-WG ({who}) und suchen ab {move} jemanden Neues.",
              "Zimmer frei in unserer gemütlichen WG an der {street}!",
              "Unsere Mitbewohnerin zieht nach Basel, darum wird ab {move} ein Zimmer frei.",
              "Hoi! Wir ({who}) suchen eine Nachmieterin oder einen Nachmieter für ein Zimmer in {area}.",
              "Per {move} vermieten wir ein Zimmer in einer {hh}er-WG nahe {area}."],
    "room": ["Das Zimmer ist {m2} m² gross, hell und {furn}.",
             "Dein Zimmer hat {m2} m², Parkett und ein grosses Fenster gegen den Innenhof.",
             "Das {furn}e Zimmer ({m2} m²) liegt ruhig auf der Rückseite des Hauses.",
             "Es gibt einen Einbauschrank, das Zimmer misst {m2} m² und hat einen kleinen Balkon.",
             "Zimmer mit {m2} m², Dachschräge und viel Licht am Morgen."],
    "life": ["Wir kochen oft zusammen und gehen am Wochenende gern wandern.",
             "Unter der Woche ist es eher ruhig, am Wochenende sind wir gesellig.",
             "Einmal im Monat machen wir einen WG-Znacht, sonst lebt jeder sein Ding.",
             "Wir teilen Putzplan und Grundeinkauf und spielen ab und zu Brettspiele.",
             "Die Küche ist gross und wird rege genutzt, wir haben auch einen Garten.",
             "Im Keller steht ein Velo-Raum, der See ist in zehn Minuten erreichbar."],
    "rules": ["Nichtraucher bevorzugt.", "Bitte keine Raucher, Haustiere leider nicht erlaubt.",
              "{dur}.", "Mindestens ein Jahr, gern länger.", "Waschmaschine im Haus, Internet inklusive."],
    "ask": ["Bitte schreib uns ein paar Zeilen über dich, wann du einziehen möchtest und wie lange du bleiben willst.",
            "Erzähl uns kurz, was du machst und was dir im WG-Leben wichtig ist.",
            "Besichtigungen am Abend möglich, schreib uns mit einem kurzen Steckbrief.",
            "Bitte nur Anfragen mit Einzugsdatum und geplanter Dauer.",
            "Melde dich mit einer kurzen Vorstellung, wir freuen uns!"],
}
WG_EN = {
    "intro": ["Room available in a friendly international flatshare of {hh} ({who}).",
              "Looking for a new flatmate from {move}!",
              "One of us is moving abroad, so a room in our flat in {area} is free from {move}.",
              "Hi! We are {who} and we are looking for someone to join our WG on {street}.",
              "Sublet from {move}: a room in a {hh}-person WG close to {area} station."],
    "room": ["The room is {m2} m², {furn_en}, with lots of light.",
             "Your room would be {m2} m² with wooden floors and a big window.",
             "The {furn_en} room ({m2} m²) faces the quiet courtyard.",
             "It has {m2} m², a built-in wardrobe and access to the balcony.",
             "Room of {m2} m² under the roof, bright in the mornings."],
    "life": ["We share dinners on Sundays and keep the place tidy.",
             "We are students and young professionals; English is our common language.",
             "Quiet during the week, social at weekends.",
             "We split cleaning and basic groceries and sometimes play board games.",
             "Big kitchen, a small garden and a bike room in the basement.",
             "The lake is ten minutes away and the tram stops right outside."],
    "rules": ["Non-smokers only.", "{dur_en}.", "No pets, sorry.", "Minimum stay one year.",
              "Washing machine in the building, internet included."],
    "ask": ["Please tell us about yourself, your move-in date and how long you would like to stay.",
            "Send us a short intro.", "Please include when you could move in.",
            "Write us a few lines about yourself and we will invite you for a viewing.",
            "Tell us what you study or do and what matters to you in a flatshare."],
}
EXTRA_DE = ["Die Nebenkosten betragen pauschal CHF {nk}.", "Haustiere nach Absprache.", "Ein Kellerabteil gehört dazu.",
            "Die Küche wurde letztes Jahr erneuert.", "Einkaufsmöglichkeiten gleich um die Ecke.",
            "Der Bahnhof ist zu Fuss in sieben Minuten erreichbar.", "Sonnige Lage mit Blick ins Grüne.",
            "Ein Velo-Raum und ein Waschsalon sind im Haus.", "Die Wohnung liegt im dritten Stock ohne Lift.",
            "Bodenheizung und Doppelverglasung, sehr ruhig.", "Ein Tiefgaragenplatz kann dazugemietet werden."]
EXTRA_EN = ["Utilities are a flat CHF {nk} per month.", "Pets on request.", "A storage room in the basement is included.",
            "The kitchen was renovated last year.", "Shops are just around the corner.",
            "The station is a seven-minute walk away.", "Sunny, with a view of the trees.",
            "Bike room and laundry in the building.", "Third floor, no lift.",
            "Floor heating and double glazing, very quiet.", "A parking space can be rented separately."]
NAMES = ["Lea", "Jonas", "Mia", "Luca", "Sara", "Noah", "Anna", "Tim", "Nina", "Elias", "Laura", "David"]


def _compose(pools: dict, rnd: random.Random, fmt: dict) -> str:
    life = rnd.sample(pools["life"], 2)
    parts = [rnd.choice(pools["intro"]), rnd.choice(pools["room"]), *life,
             rnd.choice(pools["rules"]), rnd.choice(pools["ask"])]
    return " ".join(parts).format(**fmt) + f" {rnd.choice(NAMES)}"


FLAT_DE = [
    "Schöne {rooms}-Zimmer-Wohnung ({m2} m²) an ruhiger Lage in {area}. Parkett, offene Küche, Balkon, "
    "Lift. Per {move} oder nach Vereinbarung. Wohngemeinschaften willkommen. Besichtigung auf Anfrage.",
    "Moderne {rooms}-Zimmer-Wohnung mit {m2} m², Waschturm in der Wohnung, Keller. Gute ÖV-Anbindung "
    "Richtung Zentrum und ETH. Bezug ab {move}. Bitte vollständige Bewerbung mit Betreibungsauszug.",
]
FLAT_EN = [
    "Bright {rooms}-room apartment ({m2} m²) in {area}, renovated kitchen and bathroom, close to public "
    "transport. Available from {move}. Flatshares welcome. Please send a short application.",
]
STUDIO_DE = [
    "Möbliertes Studio ({m2} m²) mit Kochnische und eigenem Bad in {area}. Ideal für Studierende. "
    "Ab {move}, Mindestmietdauer 12 Monate. Nichtraucher.",
]
STUDIO_EN = [
    "Cosy studio ({m2} m²) with kitchenette and private bathroom in {area}, 10 minutes to ETH by tram. "
    "Available from {move}, minimum 6 months.",
]
SCAMS = [
    "Beautiful room, fully furnished, all inclusive. I am currently abroad for work so I cannot show it. "
    "Please pay the deposit before the viewing via Western Union and I will send the keys by post. "
    "Contact only via WhatsApp. Many people interested, decide today.",
    "Wunderschöne Wohnung, sehr günstig! Ich bin zurzeit im Ausland und kann die Schlüssel per Post "
    "senden. Bitte zuerst die Kaution überweisen (Western Union), dann Besichtigung. Nur per WhatsApp "
    "erreichbar. Viele Interessenten, schnell entscheiden.",
]

WHO_DE = ["zwei Studentinnen und ein Doktorand", "Studierende der ETH und UZH", "zwei Berufstätige und eine Studentin"]
WHO_EN = ["two students and a nurse", "a PhD student and two engineers", "students from four countries"]

REQS_DE = ["Nichtraucher", "unbefristet, mindestens zwei Jahre", "keine Haustiere", "Studentin oder Student"]
REQS_EN = ["non-smoker", "long-term (at least one year)", "no pets", "student"]
VIBE_DE = ["gemütlich", "sozial", "ruhig", "sauber"]
VIBE_EN = ["social", "international", "tidy", "relaxed"]
ASKS_DE = ["kurze Vorstellung", "Einzugsdatum", "wie lange du bleiben willst", "was dir im WG-Leben wichtig ist"]
ASKS_EN = ["short intro", "move-in date", "how long you want to stay"]


@contextmanager
def _use_db(path: Path):
    """Point backend.db at `path` for the duration (restores the previous path)."""
    old = dbm.DB_PATH
    dbm.DB_PATH = Path(path)
    try:
        yield dbm
    finally:
        dbm.DB_PATH = old


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _make_ads(n: int, rnd: random.Random, now: datetime) -> list[dict]:
    months = [(now.replace(day=1) + timedelta(days=32 * k)).replace(day=1).strftime("%Y-%m-%d") for k in (1, 2, 3)]
    scam_at = {n // 15, n // 2 + 3} if n >= 20 else set()
    ads = []
    for i in range(n):
        area, plz, alat, alng, streets = AREAS[rnd.randrange(len(AREAS))]
        street = rnd.choice(streets)
        lat, lng = alat + rnd.uniform(-0.006, 0.006), alng + rnd.uniform(-0.008, 0.008)
        r = rnd.random()
        kind = "wg_room" if r < 0.72 else "studio" if r < 0.82 else "apartment"
        en = rnd.random() < 0.3
        move = rnd.choice(months + ["sofort"] if not en else months)
        m2 = rnd.randint(10, 24) if kind == "wg_room" else rnd.randint(20, 38) if kind == "studio" else rnd.randint(55, 120)
        rooms = None
        fmt = dict(area=area, street=street, move=move, m2=m2, hh=rnd.choice([3, 4, 5]),
                   who=rnd.choice(WHO_EN if en else WHO_DE),
                   furn=rnd.choice(["möbliert", "unmöbliert"]), furn_en=rnd.choice(["furnished", "unfurnished"]),
                   dur=rnd.choice(["Unbefristet", "Befristet bis Ende Juli", "Langfristig gesucht"]),
                   dur_en=rnd.choice(["Long-term preferred", "Fixed term until summer", "Open-ended"]))
        if kind == "wg_room":
            text = _compose(WG_EN if en else WG_DE, rnd, fmt)
            rent = rnd.randint(620, 1350)
            title = (f"Room in {fmt['hh']}-person flatshare, {area}" if en
                     else f"WG-Zimmer an der {street} ({area})")
        elif kind == "studio":
            text = rnd.choice(STUDIO_EN if en else STUDIO_DE)
            rent = rnd.randint(1150, 1850)
            title = f"Studio in {area}" if en else f"Möbliertes Studio in {area}"
        else:
            rooms = rnd.choice([2.5, 3.5, 3.5, 4.5, 4.5, 5.5])
            fmt["rooms"] = rooms
            text = rnd.choice(FLAT_EN if en else FLAT_DE)
            rent = round(((int(rooms) - 1) * rnd.randint(850, 1250) + rnd.randint(300, 700)) / 50) * 50
            title = f"{rooms}-room apartment in {area}" if en else f"{rooms}-Zimmer-Wohnung in {area}"
        if kind != "wg_room":
            text += " " + " ".join(rnd.sample(EXTRA_EN if en else EXTRA_DE, 3)) + " " + rnd.choice(NAMES)
        fmt["nk"] = rnd.choice([80, 120, 150, 180, 220, 250])
        text = text.format(**fmt)
        if i in scam_at:
            text, rent = SCAMS[len([s for s in scam_at if s < i]) % 2], 390
            kind, rooms, title = "wg_room", None, "Zimmer zentral, alles inklusive" if not en else "Room central, all inclusive"
        listed = now - timedelta(hours=rnd.uniform(0.5, 24 * 21))
        ad = dict(
            source=rnd.choice(["flatfox", "flatfox", "homegate", "woko"]) if kind != "apartment"
            else rnd.choice(["flatfox", "homegate"]),
            external_id=f"dev-{i:04d}", title=title,
            address=f"{street} {rnd.randint(1, 140)}, {plz} Zürich", lat=round(lat, 6), lng=round(lng, 6),
            rent_chf=rent, available_from=move if move != "sofort" else months[0], raw_text=text,
            url=f"https://example.invalid/listing/{i}", kind=kind, rooms=rooms, living_area_m2=m2,
            listed_at=_ts(listed), _en=en, _scam=i in scam_at,
        )
        if ad["source"] == "flatfox" and i % 9 == 4 and i not in scam_at:
            # Flatfox Priority is time-limited: most still running, every other one already over
            # (no extra random draws, so the rest of the seed is unchanged)
            ad["promo"] = "Flatfox Priority"
            ad["promo_until"] = _ts(now + timedelta(days=(i % 13) + 1) if i % 2 == 0 else now - timedelta(days=2))
        if i % 7 == 2 and i not in scam_at:
            ad["fixed_term"] = True                # befristet / temporary let
            ad["raw_text"] = "Befristet bis Ende März. " + ad["raw_text"]
        ad.update(commute_times(ad["lat"], ad["lng"]))
        ads.append(ad)
    return ads


def _extracted(ad: dict, rnd: random.Random) -> dict:
    en = ad["_en"]
    reqs = rnd.sample(REQS_EN if en else REQS_DE, rnd.randint(0, 2))
    return {
        "language": "en" if en else "de",
        "rent_chf": ad["rent_chf"],
        "move_in": ad["available_from"],
        "duration": rnd.choice(["permanent", "permanent", "fixed_term", "sublet"]) if ad["kind"] == "wg_room"
        else "permanent",
        "poster_type": "agency" if ad["kind"] == "apartment" and rnd.random() < 0.6 else "private",
        "hard_requirements": reqs,
        "preferences": rnd.sample(["social household", "clean", "student", "long-term"], 2),
        "vibe": rnd.sample(VIBE_EN if en else VIBE_DE, 2),
        "household_size": rnd.choice([2, 3, 4]) if ad["kind"] == "wg_room" else None,
        "asks_applicant_to_include": (
            rnd.sample(ASKS_EN if en else ASKS_DE, rnd.randint(1, 3)) if ad["kind"] != "apartment"
            else (["short application", "move-in date"] if en else ["Betreibungsauszug", "Einzugsdatum"])),
        "red_flags": (["deposit before viewing", "contact only via WhatsApp"] if ad["_scam"] else []),
    }


FIRST_MSG_DE = ("Hallo zusammen,\n\nich bin Student an der ETH und habe eure Anzeige gesehen. Ich koche gern, "
                "bin ruhig unter der Woche und suche ein Zimmer für längere Zeit. Hättet ihr Zeit für eine "
                "Besichtigung?\n\nViele Grüsse")
FIRST_MSG_EN = ("Hi all,\n\nI'm an ETH student and saw your ad. I like cooking, I'm quiet during the week and "
                "I'm looking for a long-term room. Could I come by for a viewing?\n\nBest regards")


def _add_outcomes(ids_by_kind: dict[str, list[int]], ads_by_id: dict[int, dict],
                  rnd: random.Random, now: datetime) -> dict:
    """Mark listings sent/replied with a stored first message 4-30 days ago. Uses the app's
    own add_sent_message/set_status, then backdates the timestamps."""
    pool = ids_by_kind.get("wg_room", [])[:60] + ids_by_kind.get("studio", [])[:6]
    # flats a 3-person group could take (4.5+ rooms), cheapest first, so group mode shows them
    flats = sorted(ids_by_kind.get("apartment", []),
                   key=lambda i: ((ads_by_id[i]["rooms"] or 0) < 4.5, ads_by_id[i]["rent_chf"]))
    chosen = pool[10:50] + flats[:4]
    n_sent = n_replied = 0
    for j, lid in enumerate(chosen):
        ad = ads_by_id[lid]
        text = (FIRST_MSG_EN if ad["_en"] else FIRST_MSG_DE) + " " + "Danke! " * (j % 7)
        dbm.add_sent_message(lid, text, "first")
        # flats stay 'sent' so group mode has follow-ups due; a third of the rest got replies
        replied = lid not in flats and j % 3 == 0
        if replied:
            dbm.set_status(lid, "replied")
            n_replied += 1
        else:
            n_sent += 1
        sent_at = now - timedelta(days=rnd.uniform(4, 30))
        # a reply comes 2 hours to 3 days later; a still-'sent' listing changed status when sent
        changed = sent_at + timedelta(hours=rnd.uniform(2, 72)) if replied else sent_at
        with dbm.get_conn() as c:
            c.execute("UPDATE sent_messages SET sent_at = ? WHERE listing_id = ?", (_ts(sent_at), lid))
            c.execute("UPDATE listings SET status_updated_at = ? WHERE id = ?", (_ts(changed), lid))
    return {"sent": n_sent, "replied": n_replied}


def build(path: str | Path, n: int = 200, seed: int = 7, outcomes: bool = False,
          profile: dict | None = None, force: bool = False) -> dict:
    """Create the DB at `path` and return a short summary dict. Refuses to overwrite an
    existing file unless force=True."""
    path = Path(path)
    if path.exists():
        if not force:
            raise FileExistsError(f"{path} exists (pass force=True / --force to replace it)")
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = profile if profile is not None else json.loads((ROOT / "profile.json").read_text())
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ads = _make_ads(n, rnd, now)
    with _use_db(path):
        dbm.init_db()
        for ad in ads:
            dbm.upsert_listing({k: v for k, v in ad.items() if not k.startswith("_")})
        with dbm.get_conn() as c:
            id_of = {r["external_id"]: r["id"] for r in c.execute("SELECT id, external_id FROM listings")}
        ads_by_id = {id_of[a["external_id"]]: a for a in ads}
        ids_by_kind: dict[str, list[int]] = {}
        for lid, a in sorted(ads_by_id.items()):
            ids_by_kind.setdefault(a["kind"], []).append(lid)

        # extraction on ~a third (all scams, as if Extract had read them)
        n_ext = 0
        for lid, a in sorted(ads_by_id.items()):
            if a["_scam"] or rnd.random() < 0.35:
                ext = _extracted(a, rnd)
                row = dict(dbm.get_listing(lid), extracted=ext)
                d = scoring.score_detail(row, profile)
                dbm.save_extraction(lid, ext, d["fit"], d["reply"])
                n_ext += 1

        outcome_info = _add_outcomes(ids_by_kind, ads_by_id, rnd, now) if outcomes else {}

        # fit/reply for everything, the way the app computes them
        rows = dbm.get_all_listings(include_raw=True, include_gone=True)
        dbm.update_scores([(lambda d: (d["fit"], d["reply"], r["id"]))(scoring.score_detail(r, profile))
                           for r in rows])

        counts = {k: len(v) for k, v in ids_by_kind.items()}
        per_source: dict = {}
        for a in ads:
            s = per_source.setdefault(a["source"], {"ok": True, "fetched": 0, "new": 0, "gone": 0})
            s["fetched"] += 1
            s["new"] += 1
        run = dbm.start_run("startup")
        dbm.finish_run(run, True, len(ads), len(ads), 0, per_source, [])
        with dbm.get_conn() as c:   # finished a few minutes ago, so it reads like a real run
            c.execute("UPDATE ingest_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                      (_ts(now - timedelta(minutes=9)), _ts(now - timedelta(minutes=4)), run))
    return {"path": str(path), "listings": len(ads), "kinds": counts, "extracted": n_ext,
            "scams": sum(1 for a in ads if a["_scam"]), **({"outcomes": outcome_info} if outcomes else {})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="SQLite file to create (never your real wgpendler.db)")
    ap.add_argument("--n", type=int, default=200, help="number of listings (default 200)")
    ap.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
    ap.add_argument("--outcomes", action="store_true", help="add sent/replied history")
    ap.add_argument("--profile", help="profile.json to score against (default: the repo's)")
    ap.add_argument("--force", action="store_true", help="replace the file if it exists")
    a = ap.parse_args(argv)
    if Path(a.path).resolve() == (ROOT / "wgpendler.db").resolve():
        ap.error("refusing to overwrite the real wgpendler.db")
    prof = json.loads(Path(a.profile).read_text()) if a.profile else None
    try:
        info = build(a.path, n=a.n, seed=a.seed, outcomes=a.outcomes, profile=prof, force=a.force)
    except FileExistsError as e:
        ap.error(str(e))
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
