"""Opt-in browser smoke test of the UI. NOT collected by pytest (name is not test_*).

    RUN_UI_SMOKE=1 python tests/ui_smoke.py                 # screenshots in <tmp>/wgp-ui-smoke/<time>/
    RUN_UI_SMOKE=1 python tests/ui_smoke.py --out /tmp/shots

Starts tools/dev_server.py on a free port (synthetic DB, isolated profile copy with an empty
message_template and a sample group, sent/replied history, fake LLM; no ingest, no network,
zero Anthropic calls), then drives headless Chromium at 1440x900 and 390x844 and checks:

  list renders and the header count matches the API     walkthrough shows on a fresh profile
  trust chips; low-trust listings hidden until opted in  detail view (+ English summary)
  draft panel and the Warm/Short tone toggle             Esc in the draft keeps the detail open,
  sync line with a mocked running /ingest/status            a second Esc closes it
  reply stats page (and the menu link to it)             group mode draft tone/language toggles
  no console errors on any page                          and the follow-up draft button
  draw-area map filter (mouse at 1440, touch taps at 390): list, pins, Message today, More
     filters count, kept over a reload, Esc cancels drawing without closing the detail, reset
  Top 5 honours Hide priority/premium, Hide befristet and the drawn area (other filters don't)
  /listings arrives gzipped; cards show match_reasons from the slim list payload; the detail
     view still loads the original ad text from /listings/{id}
  photos: detail strip (lazy, no-referrer, "Photo k of n", never in list cards), viewer with
     ‹ › / arrow keys, focus trap, Esc closes and returns focus to the thumbnail, 40px at 390;
     images come from a fake source host answered locally (no proxying, no Referer)
  a11y: icon-only controls named, landmarks, map label, debounced count announcement, Space opens
     a card and focus moves in/back, ⋯ menu arrows + Esc, focus trap + Esc focus return in panels/modals,
     40px tap targets for primary actions at 390

Needs Playwright (`pip install playwright`) and a Chromium. It uses Playwright's own browser
(PLAYWRIGHT_BROWSERS_PATH is honoured), else WGP_CHROMIUM, else /opt/pw-browsers/chromium.

Leaflet comes from unpkg.com. If that is unreachable (sandboxes, offline), the test serves
it from a local copy instead: WGP_LEAFLET_DIR, default ~/.cache/wgpendler/leaflet. Fill it once:

    mkdir -p ~/.cache/wgpendler/leaflet && cd ~/.cache/wgpendler/leaflet
    npm pack leaflet@1.9.4 leaflet.markercluster@1.5.3

(the .tgz files are unpacked on first use; never commit them). Map tiles are always answered
with a blank image so the run does not depend on Esri.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZES = [(1440, 900), (390, 844)]
LEAFLET_PKGS = [("leaflet", "1.9.4", "dist/leaflet.js"),
                ("leaflet.markercluster", "1.5.3", "dist/leaflet.markercluster.js")]
FAKE_TAG = "[fake LLM]"
# 1x1 transparent PNG for map tiles
BLANK_PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                          "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
FALLBACK_CHROMIUM = Path("/opt/pw-browsers/chromium")
PHOTO_HOST = "photos.example.invalid"   # tools/dev_seed.DEV_PHOTO_BASE


def fake_photo(url: str) -> bytes:
    """A distinct, labelled 4:3 SVG per photo URL (.../dev/<ad>/<k>[-thumb].svg)."""
    m = re.search(r"/dev/(\d+)/(\d+)(-thumb)?\.svg", url)
    ad, k = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    hue = (ad * 47 + k * 61) % 360
    w, h = (224, 168) if m and m.group(3) else (1200, 900)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 400 300">'
            f'<defs><linearGradient id="g" x2="1" y2="1"><stop offset="0" stop-color="hsl({hue},45%,42%)"/>'
            f'<stop offset="1" stop-color="hsl({(hue + 40) % 360},35%,18%)"/></linearGradient></defs>'
            f'<rect width="400" height="300" fill="url(#g)"/>'
            f'<rect x="40" y="150" width="140" height="110" fill="#0003"/><rect x="230" y="60" width="120" height="90" fill="#fff2"/>'
            f'<text x="20" y="40" font-family="monospace" font-size="24" fill="#fff">ad {ad} · photo {k}</text></svg>').encode()

failures: list[str] = []
passes = 0


def check(name: str, cond, detail="") -> bool:
    global passes
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)
    if cond:
        passes += 1
    else:
        failures.append(name)
    return bool(cond)


# ── helpers ───────────────────────────────────────────────────────────────────
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode())


def cdn_reachable() -> bool:
    try:
        with urllib.request.urlopen("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def leaflet_dir() -> Path:
    if os.environ.get("WGP_LEAFLET_DIR"):
        return Path(os.environ["WGP_LEAFLET_DIR"]).expanduser()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "wgpendler" / "leaflet"


def prepare_local_leaflet(d: Path) -> bool:
    """Make sure <d>/<pkg>@<ver>/dist/... exists, unpacking `npm pack` tarballs if needed."""
    for name, ver, probe in LEAFLET_PKGS:
        target = d / f"{name}@{ver}"
        if (target / probe).is_file():
            continue
        tgz = d / f"{name}-{ver}.tgz"
        if not tgz.is_file():
            return False
        tmp = Path(tempfile.mkdtemp(prefix="leaflet_", dir=d))
        try:
            with tarfile.open(tgz) as tf:
                if hasattr(tarfile, "data_filter"):
                    tf.extractall(tmp, filter="data")
                else:   # Python < 3.11.4: refuse anything that escapes the folder
                    for m in tf.getmembers():
                        if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                            raise RuntimeError(f"unsafe path in {tgz}: {m.name}")
                    tf.extractall(tmp)
            shutil.rmtree(target, ignore_errors=True)
            (tmp / "package").rename(target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not (target / probe).is_file():
            return False
    return True


def launch_browser(p):
    exe = os.environ.get("WGP_CHROMIUM")
    if exe:
        return p.chromium.launch(executable_path=exe)
    try:
        return p.chromium.launch()
    except Exception as e:
        if FALLBACK_CHROMIUM.exists():
            print(f"[ui] Playwright's own Chromium failed ({str(e).splitlines()[0]}); using {FALLBACK_CHROMIUM}")
            return p.chromium.launch(executable_path=str(FALLBACK_CHROMIUM))
        raise


class Pages:
    """Creates isolated browser contexts with offline routing and error collection."""

    def __init__(self, browser, base: str, local_cdn: Path | None, out: Path):
        self.browser, self.base, self.local_cdn, self.out = browser, base, local_cdn, out
        self.external: set[str] = set()
        self.photo_reqs: list[tuple[str, str | None]] = []   # (url, Referer header)

    def _unpkg(self, route):
        rel = route.request.url.split("unpkg.com/", 1)[1].split("?")[0].split("#")[0]
        f = (self.local_cdn / rel).resolve()
        if self.local_cdn in f.parents and f.is_file():
            route.fulfill(path=str(f), content_type=mimetypes.guess_type(str(f))[0] or "application/octet-stream")
        else:
            route.fulfill(status=404, body="not in local leaflet copy")

    def _other(self, route):
        url = route.request.url
        host = re.sub(r"^\w+://([^/:]+).*$", r"\1", url)
        if host in ("127.0.0.1", "localhost") or (host == "unpkg.com" and not self.local_cdn):
            return route.continue_()
        if host == "unpkg.com":
            return self._unpkg(route)
        if host == PHOTO_HOST:   # listing photos (tools/dev_seed.py): local fake images
            self.photo_reqs.append((url, route.request.headers.get("referer")))
            return route.fulfill(status=200, body=fake_photo(url), content_type="image/svg+xml",
                                 headers={"Cache-Control": "no-store"})
        self.external.add(host)
        if route.request.resource_type == "image":   # map tiles
            return route.fulfill(status=200, body=BLANK_PNG, content_type="image/png")
        return route.abort()

    def new(self, w: int, h: int, tour_done: bool = True, touch: bool = False):
        ctx = self.browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                                       service_workers="block", has_touch=touch, is_mobile=touch)
        if tour_done:
            ctx.add_init_script("try{localStorage.setItem('wgp_tour','done')}catch(e){}")
        ctx.route("**/*", self._other)
        pg = ctx.new_page()
        pg.errs, pg.reqs, pg.dialogs = [], [], []
        pg.on("pageerror", lambda e: pg.errs.append(f"pageerror: {e}"))
        pg.on("console", lambda m: pg.errs.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("request", lambda r: pg.reqs.append(f"{r.method} {r.url.replace(self.base, '')}"))
        pg.on("dialog", lambda d: (pg.dialogs.append(d.message), d.accept()))
        return pg

    def shot(self, pg, name: str, **kw):
        pg.screenshot(path=str(self.out / f"{name}.png"), **kw)


@contextmanager
def step(name: str, pg=None, pages: Pages | None = None):
    """Run a group of checks; an exception fails the step (with a screenshot) but not the run."""
    try:
        yield
    except Exception as e:
        check(f"{name}: ran without error", False, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")
        traceback.print_exc(limit=2)
        if pg is not None and pages is not None:
            try:
                pages.shot(pg, f"FAIL-{re.sub(r'[^a-z0-9]+', '-', name.lower())}")
            except Exception:
                pass


def is_open(pg) -> bool:
    return pg.evaluate("$('detail-view').classList.contains('open')")


def wait_list(pg, base):
    pg.goto(base + "/", wait_until="domcontentloaded")
    pg.wait_for_selector(".card", timeout=20000)
    pg.wait_for_function("loadState === 'ok' && /\\d+ of \\d+/.test($('count').textContent)", timeout=20000)
    pg.wait_for_timeout(400)


# ── the checks ────────────────────────────────────────────────────────────────
def run_size(pages: Pages, base: str, w: int, h: int, api: dict):
    tag = str(w)
    solo_total = len(api["solo"])
    low_ids = {l["id"] for l in api["solo"] if l.get("trust_level") == "low"}
    de_pick = next(l["id"] for l in api["solo"]
                   if (l.get("extracted") or {}).get("language") == "de" and not l.get("is_dealbreaker")
                   and l.get("trust_level") != "low" and not l.get("status"))

    # 1. first-run walkthrough (fresh browser, profile has no message_template)
    pg = pages.new(w, h, tour_done=False)
    with step(f"{tag} walkthrough", pg, pages):
        wait_list(pg, base)
        pg.wait_for_selector("#hint.on", timeout=5000)
        check(f"{tag} walkthrough visible on a fresh profile",
              pg.is_visible("#hint") and "Step 1/3" in pg.text_content("#hint-step"))
        pages.shot(pg, f"{tag}-01-walkthrough")
        for _ in range(3):
            pg.click("#hint-next")
        check(f"{tag} walkthrough closes after 3 steps and is remembered",
              not pg.is_visible("#hint") and pg.evaluate("localStorage.getItem('wgp_tour')") == "done")
    check(f"{tag} walkthrough page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()

    # 2. main page
    pg = pages.new(w, h)
    with step(f"{tag} list", pg, pages):
        wait_list(pg, base)
        cnt = pg.text_content("#count").strip()
        m = re.fullmatch(r"([\d,]+) of ([\d,]+)", cnt)
        shown, total = (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))) if m else (-1, -1)
        check(f"{tag} header count is 'N of M'", bool(m), cnt)
        check(f"{tag} header total matches API ({solo_total})", total == solo_total, cnt)
        check(f"{tag} header shown matches the filtered list",
              0 < shown <= total and shown == pg.evaluate("curList.length"), cnt)
        check(f"{tag} cards rendered", pg.locator(".card").count() > 0)
        check(f"{tag} alerts state loads without the pending count (?pending=0)",
              any(r.endswith("/notify/status?pending=0") for r in pg.reqs)
              and not any(r.endswith("/notify/status") for r in pg.reqs), [r for r in pg.reqs if "notify" in r])
        check(f"{tag} walkthrough hidden once done", not pg.is_visible("#hint"))
        enc = pg.evaluate("fetch('/listings?kind=wg_room,studio').then(r => r.headers.get('content-encoding'))")
        check(f"{tag} /listings arrives gzipped", enc == "gzip", enc)
        check(f"{tag} cards show the server's match reasons (list payload has no fit_reasons alias)",
              pg.evaluate("curList.length > 0 && curList.every(l => !('fit_reasons' in l)) && "
                          "curList.filter(l => (l.match_reasons || []).length)"
                          ".every(l => JSON.stringify(fitReasons(l)) === "
                          "JSON.stringify(l.match_reasons.map(String).slice(0, 4)))"))
        check(f"{tag} map has markers", pg.locator(".leaflet-marker-icon").count() > 0)
        pages.shot(pg, f"{tag}-02-list")

    with step(f"{tag} trust", pg, pages):
        check(f"{tag} trust chips on cards", pg.locator(".card .trust-chip").count() > 0)
        check(f"{tag} seed has low-trust (scam) listings", len(low_ids) >= 1)
        cur = set(pg.evaluate("curList.map(l => l.id)"))
        check(f"{tag} low-trust hidden by default", not (cur & low_ids), sorted(cur & low_ids))
        pg.evaluate("$('showLow').checked = true; applyFilters()")
        pg.wait_for_timeout(300)
        cur = set(pg.evaluate("curList.map(l => l.id)"))
        check(f"{tag} 'Show low-trust' reveals them", low_ids <= cur, sorted(low_ids - cur))
        low = sorted(low_ids)[0]
        pg.evaluate(f"selectListing({low})")
        pg.wait_for_selector(".trust-warn", timeout=5000)
        check(f"{tag} low-trust detail shows the scam warning", "possible scam" in pg.text_content(".trust-warn"))
        pg.evaluate("document.querySelector('.trust-warn').scrollIntoView({block:'center'})")
        pages.shot(pg, f"{tag}-03-trust-warning")
        pg.keyboard.press("Escape")
        # hide the pin right away, while zoomToShowLayer may still be moving to it: this used
        # to leave markercluster with a dangling moveend listener that broke every later
        # selectListing (the next step opens a detail, so it would fail)
        pg.evaluate("$('showLow').checked = false; applyFilters()")
        pg.wait_for_timeout(1200)
        check(f"{tag} hidden low-trust pin leaves the map after a fast filter change",
              pg.evaluate(f"!shown.has({low}) && !cluster.hasLayer(markers[{low}])"))

    with step(f"{tag} priority filter and Message today", pg, pages):
        check(f"{tag} seed has running and expired priority listings",
              pg.evaluate("allListings.some(l => promoActive(l)) && allListings.some(l => l.promo && !promoActive(l))"))
        pg.evaluate("setShow('sent')")
        pg.wait_for_timeout(300)
        check(f"{tag} Message today stays visible outside the To do view", pg.evaluate("!$('today').hidden"))
        pg.evaluate("setShow('todo')")
        pg.evaluate("$('hidePromo').checked = true; savePromo(); applyFilters()")
        pg.wait_for_timeout(400)
        check(f"{tag} 'Hide priority/premium' removes them from the list",
              pg.evaluate("curList.length > 0 && !curList.some(l => promoActive(l))"))
        check(f"{tag} ...but keeps listings whose Priority period is over",
              pg.evaluate("curList.some(l => l.promo && !promoActive(l)) || !allListings.some(l => l.promo && !promoActive(l) && passes(l, {..._todayF, hidePromo: false}))"))
        check(f"{tag} ...and from Message today",
              pg.evaluate("!$('today').hidden && ![...document.querySelectorAll('.today-row')].some(r => promoActive(byId[+r.dataset.id]))"))
        pg.evaluate("$('hidePromo').checked = false; savePromo(); applyFilters()")
        pg.wait_for_timeout(300)
        check(f"{tag} fixed-term listings carry a 'befristet' chip",
              pg.evaluate("curList.some(l => isFixedTerm(l))") and pg.locator(".term-chip").count() > 0)
        pg.evaluate("$('hideTerm').checked = true; savePromo(); applyFilters()")
        pg.wait_for_timeout(400)
        check(f"{tag} 'Hide befristet' removes them from the list",
              pg.evaluate("curList.length > 0 && !curList.some(l => isFixedTerm(l))"))
        check(f"{tag} ...and phone alerts learn it (server-side filter)",
              get_json(base, "/notify/status").get("filters") == {"hide_promo": False, "hide_fixed_term": True})
        pg.evaluate("$('hideTerm').checked = false; savePromo(); applyFilters()")
        pg.wait_for_timeout(300)
        check(f"{tag} ...and unticking clears it for alerts",
              get_json(base, "/notify/status").get("filters") == {"hide_promo": False, "hide_fixed_term": False})

    with step(f"{tag} detail", pg, pages):
        first = pg.locator(".card").first
        title = pg.evaluate("byId[+document.querySelector('.card').dataset.id].title")
        first.click()
        pg.wait_for_function("$('detail-view').classList.contains('open')", timeout=5000)
        check(f"{tag} clicking a card opens the detail view", is_open(pg))
        check(f"{tag} detail shows the listing title", title.split()[0] in pg.text_content("#detail-view"), title)
        try:   # /listings leaves raw_text out; the detail fetches it from /listings/{id}
            pg.wait_for_selector("#rawtext-slot details.rawtext", state="attached", timeout=5000)
        except Exception:
            pass
        check(f"{tag} detail loads the original ad text", pg.locator("#rawtext-slot pre").count() == 1
              and len(pg.text_content("#rawtext-slot pre") or "") > 20)
        pages.shot(pg, f"{tag}-04-detail")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        check(f"{tag} Esc closes the detail", not is_open(pg))

    with step(f"{tag} move-in from the ad text", pg, pages):
        # seed: date field 2004-MM-DD (live Flatfox typo), text "Room available from 1 November"
        mid = pg.evaluate("(allListings.find(l => l.move_in_stated && /^2004/.test(l.available_from)) || {}).id")
        check(f"{tag} seed has a listing with an implausible date field", bool(mid))
        if mid:
            card = pg.evaluate(f"(() => {{ const l = byId[{mid}], d = document.createElement('div');"
                               f" d.innerHTML = cardHtml(l); return [d.textContent, l.move_in_stated,"
                               f" d.querySelector('.movein-text')?.title || ''] }})()")
            check(f"{tag} card shows the text's move-in date, not the 2004 field",
                  f"from {card[1]}*" in card[0] and "2004" not in card[0], card[0][:120])
            check(f"{tag} card tooltip names the ad's date field", "2004" in card[2], card[2])
            pg.evaluate(f"selectListing({mid})")
            pg.wait_for_function("$('detail-view').classList.contains('open')", timeout=5000)
            note = pg.text_content("#detail-view .movein-note") if pg.locator("#detail-view .movein-note").count() else ""
            check(f"{tag} detail explains the date came from the ad text", "2004" in note, note)
            pages.shot(pg, f"{tag}-04b-movein-detail")
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(300)

    with step(f"{tag} draft", pg, pages):
        pg.evaluate(f"selectListing({de_pick})")
        pg.wait_for_function("$('detail-view').classList.contains('open')", timeout=5000)
        pg.wait_for_selector("#ad-summary:not([hidden])", timeout=5000)
        check(f"{tag} English summary of a German ad (fake LLM)", FAKE_TAG in pg.text_content("#ad-summary"))
        pg.click("#draft-btn")
        pg.wait_for_function(f"$('draft-text') && $('draft-text').value.includes({json.dumps(FAKE_TAG)})", timeout=10000)
        warm = pg.input_value("#draft-text")
        check(f"{tag} draft panel shows the drafted text", len(warm.split()) > 30, warm[:60])
        segs = pg.locator(".dseg").first.locator("button")
        check(f"{tag} tone toggle Warm|Short, Warm pressed",
              segs.count() == 2 and segs.nth(0).get_attribute("aria-pressed") == "true", segs.all_text_contents())
        check(f"{tag} draft checklist rendered", pg.locator("#draft-check li").count() > 0)
        pg.evaluate("$('draft-box').scrollIntoView({block:'nearest'})")
        pages.shot(pg, f"{tag}-05-draft-warm")
        segs.nth(1).click()
        pg.wait_for_function("$('draft-text') && $('draft-text').value.split(/\\s+/).length < 40", timeout=10000)
        check(f"{tag} Short tone requests tone=short and shows a shorter draft",
              any(f"/draft/{de_pick}?tone=short" in r for r in pg.reqs)
              and pg.locator(".dseg").first.locator("button").nth(1).get_attribute("aria-pressed") == "true")
        pg.evaluate("$('draft-box').scrollIntoView({block:'nearest'})")
        pages.shot(pg, f"{tag}-06-draft-short")

    with step(f"{tag} esc in draft", pg, pages):
        pg.focus("#draft-text")
        pg.keyboard.press("End")
        pg.keyboard.type(" Test")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(250)
        check(f"{tag} Esc in the draft textarea keeps the detail open", is_open(pg))
        check(f"{tag} ... leaves the textarea and keeps the edit",
              pg.evaluate("document.activeElement !== $('draft-text') && $('draft-text').value.endsWith(' Test')"))
        pages.shot(pg, f"{tag}-07-esc-once")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        check(f"{tag} second Esc closes the detail", not is_open(pg))

    with step(f"{tag} stats link", pg, pages):
        pg.click("#more-btn")
        pg.get_by_role("menuitem", name="Reply stats").click()
        pg.wait_for_url("**/stats.html", timeout=5000)
        check(f"{tag} menu > Reply stats opens /stats.html", pg.url.endswith("/stats.html"))
    check(f"{tag} main page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    if pg.dialogs:
        print(f"[ui] {tag} dialogs seen: {pg.dialogs}")
    pg.context.close()

    # 3. sync line with a running ingest (mocked)
    pg = pages.new(w, h)
    with step(f"{tag} sync", pg, pages):
        st = {"running": True, "sources_total": 5, "sources_done": ["flatfox"], "current": "homegate",
              "count": 412, "new": 3, "last_finished_at": None, "recent_runs": []}
        pg.route("**/ingest/status", lambda r: r.fulfill(status=200, content_type="application/json",
                                                           body=json.dumps(st)))
        wait_list(pg, base)
        pg.wait_for_selector("#sync.on", timeout=10000)
        txt = pg.text_content("#sync")
        check(f"{tag} sync line shows progress and current source", txt == "syncing 2/5 · homegate", txt)
        if w > 760:   # on phones the sync line replaces the count until it ends (by design)
            check(f"{tag} header count still visible while syncing", pg.is_visible("#count"))
        pages.shot(pg, f"{tag}-08-sync", clip={"x": 0, "y": 0, "width": w, "height": 120})
    check(f"{tag} sync page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()

    # 4. reply stats page
    pg = pages.new(w, h)
    with step(f"{tag} stats", pg, pages):
        pg.goto(base + "/stats.html", wait_until="domcontentloaded")
        pg.wait_for_selector("#tiles .tile", timeout=10000)
        pg.wait_for_timeout(500)
        tiles = pg.locator("#tiles .tile").count()
        check(f"{tag} stats page renders summary tiles", tiles >= 3, tiles)
        body = pg.text_content("body")
        check(f"{tag} stats page shows seeded outcomes (by source)", re.search(r"flatfox", body, re.I) is not None)
        check(f"{tag} stats page has no horizontal scroll",
              not pg.evaluate("document.documentElement.scrollWidth > innerWidth"))
        pages.shot(pg, f"{tag}-09-stats", full_page=True)
    check(f"{tag} stats page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()

    # 5. group mode follow-up
    pg = pages.new(w, h)
    with step(f"{tag} group", pg, pages):
        wait_list(pg, base)
        pg.click("#mode-seg button[data-m=group]")
        pg.wait_for_function("groupMode && loadState === 'ok' && document.querySelector('.card')", timeout=15000)
        pg.wait_for_timeout(400)
        check(f"{tag} group mode lists flats for the group",
              "group of" in pg.text_content("#sh-summary") and pg.locator(".card").count() > 0)
        check(f"{tag} group minus reasons (over budget, unknown, red flags) are amber '!' chips, not ticks",
              pg.evaluate("['CHF 1350 pp over budget', 'room count unknown', '2 red flags'].every(r => "
                          "/chip warn/.test(reasonChip(r)) && !reasonChip(r).includes('✓'))"
                          " && /✓/.test(reasonChip('CHF 900 pp within budget'))"
                          " && ![...document.querySelectorAll('.card .chip')].some(c => "
                          "c.textContent.startsWith('✓') && MINUS_REASON.test(c.textContent))"))
        pages.shot(pg, f"{tag}-10-group-list")

    with step(f"{tag} group draft options", pg, pages):
        gid = api["group_draft"]
        pg.evaluate(f"selectListing({gid})")
        pg.wait_for_selector("#draft-btn", state="visible", timeout=8000)
        pg.click("#draft-btn")
        pg.wait_for_function(f"$('draft-text') && $('draft-text').value.includes({json.dumps(FAKE_TAG)})",
                             timeout=10000)
        warm = pg.input_value("#draft-text")
        segs = pg.locator("#draft-box .dseg").first.locator("button")
        check(f"{tag} group draft shows the Warm|Short toggle, Warm pressed",
              segs.count() == 2 and segs.nth(0).get_attribute("aria-pressed") == "true", segs.all_text_contents())
        check(f"{tag} group draft requests /group/draft/{gid}?tone=warm&lang=auto",
              any(f"POST /group/draft/{gid}?tone=warm&lang=auto" in r for r in pg.reqs))
        pg.evaluate("$('draft-box').scrollIntoView({block:'nearest'})")
        pages.shot(pg, f"{tag}-10b-group-draft-warm")
        segs.nth(1).click()
        pg.wait_for_function(f"$('draft-text') && $('draft-text').value !== {json.dumps(warm)}", timeout=10000)
        check(f"{tag} group Short sends tone=short and shows a shorter draft",
              any(f"POST /group/draft/{gid}?tone=short" in r for r in pg.reqs)
              and len(pg.input_value("#draft-text").split()) < len(warm.split())
              and pg.locator("#draft-box .dseg").first.locator("button").nth(1).get_attribute("aria-pressed") == "true")
        pg.evaluate("$('draft-box').scrollIntoView({block:'nearest'})")
        pages.shot(pg, f"{tag}-10c-group-draft-short")
        lseg = pg.locator("#draft-box .dseg").nth(1).locator("button")
        check(f"{tag} group draft shows the language toggle (seeded English flat, German-first profile)",
              lseg.count() == 2, pg.text_content("#draft-box .draft-opts"))
        lseg.nth(1).click()
        pg.wait_for_function(f"$('draft-text') && $('draft-text').value.includes({json.dumps(FAKE_TAG)})"
                             " && document.querySelectorAll('#draft-box .dseg')[1]"
                             ".querySelectorAll('button')[1].getAttribute('aria-pressed') === 'true'", timeout=10000)
        check(f"{tag} group language toggle sends tone=short&lang=de",
              any(f"POST /group/draft/{gid}?tone=short&lang=de" in r for r in pg.reqs))

    with step(f"{tag} group follow-up", pg, pages):
        fid = api["group_followup"]
        check(f"{tag} seed has a flat with a follow-up due", fid is not None)
        pg.evaluate(f"selectListing({fid})")
        pg.wait_for_selector("#fu-btn", state="visible", timeout=8000)
        check(f"{tag} group detail shows 'Draft follow-up'", pg.is_visible("#fu-btn"))
        pg.click("#fu-btn")
        pg.wait_for_function(f"$('draft-text') && $('draft-text').value.includes({json.dumps(FAKE_TAG)})",
                             timeout=10000)
        check(f"{tag} follow-up draft via /group/draft/{fid}?followup=1",
              any(f"POST /group/draft/{fid}?followup=1" in r for r in pg.reqs)
              and "Follow-up draft" in pg.text_content("#draft-box .draft-head"))
        warn = pg.text_content("#draft-warn")
        check(f"{tag} follow-up language is checked against the first message", warn == "matches your first message",
              warn)
        pg.evaluate("$('draft-box').scrollIntoView({block:'nearest'})")
        pages.shot(pg, f"{tag}-11-group-followup")
    check(f"{tag} group page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()


AREA_JS = """() => {   // pixel box (page coords) around the densest bit of the current list
  const box = $('map').getBoundingClientRect(), ctl = document.querySelector('.area-ctl').getBoundingClientRect();
  const clearOfCtl = p => box.left + p.x + 45 < ctl.left - 10 || box.top + p.y - 35 > ctl.bottom + 50;   // + room for the tip
  const pts = curList.map(l => map.latLngToContainerPoint([l.lat, l.lng]))
    .filter(p => p.x > 50 && p.x < box.width - 50 && p.y > 45 && p.y < box.height - 45 && clearOfCtl(p));
  let best = null, bestN = -1;
  for (const c of pts) { const n = pts.filter(p => Math.abs(p.x - c.x) < 45 && Math.abs(p.y - c.y) < 35).length; if (n > bestN) { best = c; bestN = n; } }
  const x0 = best.x - 45, x1 = best.x + 45, y0 = best.y - 35, y1 = best.y + 35;
  return { corners: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(([x, y]) => [box.left + x, box.top + y]), n: bestN };
}"""


# ── accessibility / keyboard ──────────────────────────────────────────────────
ICON_ONLY = """() => [...document.querySelectorAll('button, a[href], [role=button]')]
  .filter(el => el.getClientRects().length && !el.closest('[hidden]'))
  .filter(el => !/[A-Za-z0-9]/.test(el.textContent) && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby'))
  .map(el => el.outerHTML.slice(0, 80))"""
SMALL_TAPS = """() => [...document.querySelectorAll('#topbar button, .card-actions .st, .card-actions .act, .detail-back, #draft-btn, .draft-actions button')]
  .filter(el => el.getClientRects().length && !el.closest('[hidden]'))
  .map(el => { const r = el.getBoundingClientRect(), a = getComputedStyle(el, '::after');
               const extra = a.content !== 'none' ? Math.max(0, -parseFloat(a.top || 0)) + Math.max(0, -parseFloat(a.bottom || 0)) : 0;
               return [el.id || el.className, Math.round(r.height + extra)]; })
  .filter(([, h]) => h < 40)"""


def focused(pg) -> str:
    return pg.evaluate("(() => { const a = document.activeElement; return a ? (a.id || a.className || a.tagName) : ''; })()")


def run_a11y(pages: Pages, base: str, w: int, h: int):
    tag = f"{w} a11y"
    pg = pages.new(w, h, touch=w < 760)
    with step(f"{tag} names and landmarks", pg, pages):
        wait_list(pg, base)
        check(f"{tag} icon-only controls have an aria-label", pg.evaluate(ICON_ONLY) == [], pg.evaluate(ICON_ONLY)[:3])
        check(f"{tag} one <main>, a <header> and a labelled map",
              pg.locator("main").count() == 1 and pg.locator("header#topbar").count() == 1
              and bool(pg.get_attribute("#map", "aria-label")))
        check(f"{tag} the visible count is not a live region (a debounced sr-only one is)",
              pg.get_attribute("#count", "aria-live") is None and pg.get_attribute("#count-live", "aria-live") == "polite")
        check(f"{tag} ETH anchor pins are not unnamed tab stops",
              pg.evaluate("""[...document.querySelectorAll('.leaflet-marker-icon[role=button]')]
                .filter(el => !el.textContent.trim() && !el.getAttribute('aria-label')).length""") == 0)
        if w < 760:
            check(f"{tag} primary actions are at least 40px tall (list)", pg.evaluate(SMALL_TAPS) == [], pg.evaluate(SMALL_TAPS)[:4])

    with step(f"{tag} live count", pg, pages):
        if w < 760:
            pg.click("#filters-toggle")
        pg.evaluate("""() => { window._liveN = 0; new MutationObserver(() => window._liveN++).observe($('count-live'), { childList: true, characterData: true, subtree: true }); }""")
        pg.focus("#maxRent")
        for _ in range(6):
            pg.keyboard.press("ArrowLeft")
        pg.wait_for_timeout(1500)
        n = pg.evaluate("window._liveN")
        check(f"{tag} six slider steps announce the count once", n == 1, n)
        check(f"{tag} ...with the final count", pg.text_content("#count-live").startswith(pg.text_content("#count").strip()),
              pg.text_content("#count-live"))
        pg.evaluate("resetFilters()")
        if w < 760:
            pg.click("#filters-toggle")

    with step(f"{tag} cards by keyboard", pg, pages):
        card = pg.locator(".card").first
        cid = card.get_attribute("id")
        card.focus()
        pg.keyboard.press(" ")
        pg.wait_for_timeout(300)
        check(f"{tag} Space on a focused card opens the detail", is_open(pg))
        check(f"{tag} ...and focus moves into the detail (back button)", "detail-back" in focused(pg), focused(pg))
        if w < 760:
            check(f"{tag} primary actions are at least 40px tall (detail)", pg.evaluate(SMALL_TAPS) == [], pg.evaluate(SMALL_TAPS)[:4])
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        check(f"{tag} Esc closes the detail and focus returns to the card", not is_open(pg) and focused(pg) == cid,
              focused(pg))

    with step(f"{tag} menu", pg, pages):
        pg.focus("#more-btn")
        pg.keyboard.press("Enter")
        first = focused(pg)
        check(f"{tag} ⋯ opens the menu and focuses the first item", pg.is_visible("#more-menu") and first == "extract-btn", first)
        pg.keyboard.press("ArrowDown")
        check(f"{tag} ArrowDown moves to the next item", focused(pg) != first)
        pg.keyboard.press("Escape")
        check(f"{tag} Esc closes the menu and focus returns to ⋯", not pg.is_visible("#more-menu") and focused(pg) == "more-btn",
              focused(pg))

    with step(f"{tag} modals", pg, pages):
        for opener, box, closed in [("#profile-btn", "#profile-panel", "!$('profile-panel').classList.contains('open')"),
                                    ("#more-btn", "#help-box", "$('help-modal').hidden")]:
            pg.focus(opener)
            if opener == "#more-btn":   # Keyboard shortcuts via the menu: focus must come back to ⋯, not the hidden item
                pg.keyboard.press("Enter")
                pg.focus("#more-menu button:has-text('Keyboard shortcuts')")
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(350)
            inside = []
            for _ in range(25):
                pg.keyboard.press("Tab")
                inside.append(pg.evaluate(f"document.querySelector('{box}').contains(document.activeElement)"))
            check(f"{tag} Tab stays inside {box} (focus trap)", all(inside), inside.count(False))
            pg.keyboard.press("Shift+Tab")
            check(f"{tag} Shift+Tab stays inside {box}", pg.evaluate(f"document.querySelector('{box}').contains(document.activeElement)"))
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(250)
            want = opener.lstrip("#")
            check(f"{tag} Esc closes {box} and focus returns to {opener}", pg.evaluate(closed) and focused(pg) == want, focused(pg))
    check(f"{tag} page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()


def run_photos(pages: Pages, base: str, w: int, h: int, api: dict):
    tag = f"{w} photos"
    lid, n = api.get("photo_listing") or (None, 0)
    if not check(f"{tag}: the dev seed has a listing with photos", lid is not None):
        return
    pg = pages.new(w, h, touch=w < 760)
    start = len(pages.photo_reqs)
    with step(f"{tag} strip", pg, pages):
        wait_list(pg, base)
        check(f"{tag} list cards show no photos", pg.locator("#cards img").count() == 0)
        check(f"{tag} no photo requested before a detail opens", len(pages.photo_reqs) == start)
        pg.evaluate(f"selectListing({lid})")
        pg.wait_for_function(f"document.querySelectorAll('#photo-strip .photo-thumb').length === {n}", timeout=10000)
        imgs = pg.evaluate("""[...document.querySelectorAll('#photo-strip img')].map(i => [i.getAttribute('loading'),
            i.getAttribute('decoding'), i.getAttribute('referrerpolicy'), i.alt])""")
        check(f"{tag} {n} thumbnails, lazy + async + no-referrer", all(i[:3] == ["lazy", "async", "no-referrer"] for i in imgs), imgs[:2])
        check(f"{tag} thumbnail alt text 'Photo k of n'", [i[3] for i in imgs] == [f"Photo {k} of {n}" for k in range(1, n + 1)],
              [i[3] for i in imgs][:3])
        strip = pg.evaluate("(() => { const s = $('photo-strip'); return [s.scrollWidth, s.clientWidth, getComputedStyle(s).overflowX]; })()")
        check(f"{tag} the strip scrolls sideways (overflow-x auto), the page does not",
              strip[2] == "auto" and pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), strip)
        pg.wait_for_function("[...document.querySelectorAll('#photo-strip img')].some(i => i.complete && i.naturalWidth > 0)", timeout=10000)
        hs = pg.evaluate("[...document.querySelectorAll('.photo-thumb')].map(b => Math.round(b.getBoundingClientRect().height))")
        check(f"{tag} thumbnails are at least 40px tap targets", min(hs) >= 40, hs[:3])
        loaded = [u for u, _ in pages.photo_reqs[start:]]
        check(f"{tag} photos load straight from the source host (not via the app)",
              loaded and all(u.startswith(f"https://{PHOTO_HOST}/") for u in loaded)
              and not any("/dev/" in r for r in pg.reqs if not r.split(" ", 1)[1].startswith("http")), loaded[:2])
        check(f"{tag} no Referer sent to the image host", all(not r for _, r in pages.photo_reqs[start:]),
              [r for _, r in pages.photo_reqs[start:] if r][:1])
        pages.shot(pg, f"photos-strip-{w}")

    with step(f"{tag} viewer", pg, pages):
        thumb = pg.locator('#photo-strip .photo-thumb[data-photo="1"]')
        thumb.scroll_into_view_if_needed()
        thumb.focus()
        pg.keyboard.press("Enter")
        pg.wait_for_function("!$('photo-viewer').hidden && $('pv-img').complete && $('pv-img').naturalWidth > 0", timeout=10000)
        check(f"{tag} Enter on photo 2 opens the viewer at 'Photo 2 / {n}'",
              pg.text_content("#pv-count") == f"Photo 2 / {n}" and pg.get_attribute("#pv-img", "alt") == f"Photo 2 of {n}",
              pg.text_content("#pv-count"))
        check(f"{tag} the viewer is a labelled modal dialog with focus inside",
              pg.get_attribute("#photo-viewer", "role") == "dialog" and pg.get_attribute("#photo-viewer", "aria-modal") == "true"
              and pg.evaluate("$('photo-viewer').contains(document.activeElement)"))
        check(f"{tag} the full-size image is loaded without a referrer",
              pg.get_attribute("#pv-img", "referrerpolicy") == "no-referrer" and pg.get_attribute("#pv-img", "src").endswith("/2.svg"))
        big = pg.evaluate("[...document.querySelectorAll('#photo-viewer button')].map(b => { const r = b.getBoundingClientRect(); return [b.getAttribute('aria-label'), Math.round(r.width), Math.round(r.height)]; })")
        if w < 760:
            check(f"{tag} viewer buttons are at least 40x40", all(bw >= 40 and bh >= 40 for _, bw, bh in big), big)
        check(f"{tag} viewer buttons are named", all(lbl for lbl, _, _ in big), big)
        pages.shot(pg, f"photos-viewer-{w}")
        pg.keyboard.press("ArrowRight")
        check(f"{tag} ArrowRight shows the next photo", pg.text_content("#pv-count") == f"Photo 3 / {n}", pg.text_content("#pv-count"))
        pg.click("#pv-prev")
        pg.click("#pv-prev")
        check(f"{tag} ‹ goes back and is disabled on the first photo",
              pg.text_content("#pv-count") == f"Photo 1 / {n}" and pg.is_disabled("#pv-prev"), pg.text_content("#pv-count"))
        check(f"{tag} focus moves off the disabled ‹ button", pg.evaluate("$('photo-viewer').contains(document.activeElement) && !document.activeElement.disabled"))
        pg.click("#pv-next")
        inside = []
        for _ in range(8):
            pg.keyboard.press("Tab")
            inside.append(pg.evaluate("$('photo-viewer').contains(document.activeElement)"))
        check(f"{tag} Tab stays inside the viewer (focus trap)", all(inside), inside.count(False))
        pg.keyboard.press("j")
        check(f"{tag} list shortcuts are off while the viewer is open", pg.evaluate(f"selectedId === {lid}"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(150)
        check(f"{tag} Esc closes the viewer, keeps the detail and returns focus to photo 2",
              pg.evaluate("$('photo-viewer').hidden") and is_open(pg)
              and pg.evaluate("document.activeElement?.dataset?.photo") == "1", focused(pg))
        if w < 760:
            thumb.tap()
            pg.wait_for_function("!$('photo-viewer').hidden")
            check(f"{tag} tapping a thumbnail opens the viewer", pg.text_content("#pv-count") == f"Photo 2 / {n}")
            pg.click("#photo-viewer .cancel-btn")
            check(f"{tag} ✕ closes the viewer", pg.evaluate("$('photo-viewer').hidden") and is_open(pg))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(150)
        check(f"{tag} a second Esc closes the detail as before", not is_open(pg))
    check(f"{tag} page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()


def run_area(pages: Pages, base: str, w: int, h: int):
    tag, touch = str(w), w <= 760
    pg = pages.new(w, h, touch=touch)
    with step(f"{tag} draw area", pg, pages):
        wait_list(pg, base)
        btn = pg.locator(".area-ctl button#area-draw")
        check(f"{tag} 'Draw area' is a real button with an aria-label",
              btn.count() == 1 and bool(btn.get_attribute("aria-label")))
        before = pg.evaluate("curList.map(l => l.id)")
        if touch:
            btn.tap()
        else:
            btn.click()
        box = pg.evaluate(AREA_JS)   # after the Done/Cancel row and tip have appeared
        check(f"{tag} draw mode on (crosshair class, Done/Cancel shown)",
              pg.evaluate("drawPts !== null && $('map').classList.contains('drawing')") and pg.is_visible("#area-cancel"))
        c = box["corners"]
        pages.shot(pg, f"{tag}-12a-draw-mode")
        if touch:   # taps; tapping the first point again closes the shape
            for x, y in c + [c[0]]:
                pg.touchscreen.tap(x, y)
                pg.wait_for_timeout(450)   # a human pace; quicker taps can merge into a double-tap
        else:       # clicks; a double-click on the last corner closes it
            for x, y in c[:3]:
                pg.mouse.click(x, y)
                pg.wait_for_timeout(80)
            pg.mouse.dblclick(*c[3])
        pg.wait_for_function("area && !drawPts", timeout=3000)
        pg.wait_for_timeout(400)
        after = pg.evaluate("curList.map(l => l.id)")
        check(f"{tag} area closes with {'a tap on the first point' if touch else 'a double-click'} (4 corners)",
              pg.evaluate("area.length") == 4, pg.evaluate("JSON.stringify(area)"))
        check(f"{tag} list shrinks to the listings inside the area",
              0 < len(after) < len(before) and set(after) <= set(before), f"{len(after)} of {len(before)}")
        check(f"{tag} ...exactly those inside",
              pg.evaluate("curList.every(l => WGGeo.pointInPolygon(l.lat, l.lng, area))")
              and pg.evaluate(f"{json.dumps(before)}.filter(id => WGGeo.pointInPolygon(byId[id].lat, byId[id].lng, area)).length")
              == len(after))
        check(f"{tag} pins outside the area leave the map",
              pg.evaluate("[...shown].every(id => inArea(byId[id])) && shown.size === curList.length"))
        check(f"{tag} Message today only picks inside the area",
              pg.evaluate("[...document.querySelectorAll('.today-row')].every(r => inArea(byId[+r.dataset.id]))"))
        check(f"{tag} area counts in 'More filters (n)' and the list header",
              pg.text_content("#more-count").strip() == "(1)" and "in drawn area" in pg.text_content("#sh-summary"))
        check(f"{tag} dashed area outline drawn, 'Clear area' button shown",
              pg.evaluate("areaLayer.getLayers().length") == 1 and pg.is_visible("#area-clear"))
        pages.shot(pg, f"{tag}-12b-draw-area")
        # kept over a reload
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_function("loadState === 'ok' && /\\d+ of \\d+/.test($('count').textContent)", timeout=20000)
        pg.wait_for_timeout(400)
        check(f"{tag} area remembered after a reload",
              sorted(pg.evaluate("curList.map(l => l.id)")) == sorted(after) and pg.is_visible("#area-clear"))
        # Clear area
        if touch:
            pg.locator("#area-clear").tap()
        else:
            pg.click("#area-clear")
        pg.wait_for_timeout(400)
        check(f"{tag} 'Clear area' restores the full list",
              pg.evaluate("curList.length") == len(before) and pg.evaluate("localStorage.getItem('wgp_area')") is None
              and pg.text_content("#more-count").strip() == "")
        # Esc while drawing cancels the drawing only; the open detail stays
        pg.evaluate(f"selectListing({before[0]})")
        pg.wait_for_function("$('detail-view').classList.contains('open')", timeout=5000)
        pg.evaluate("startDraw()")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        check(f"{tag} Esc while drawing cancels it and keeps the detail open",
              pg.evaluate("drawPts === null && !area && !$('map').classList.contains('drawing')") and is_open(pg))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        check(f"{tag} ...a second Esc closes the detail", not is_open(pg))
        # reset clears an area
        pg.evaluate("setArea(" + json.dumps([[47.37, 8.53], [47.37, 8.55], [47.39, 8.55], [47.39, 8.53]]) + ")")
        pg.evaluate("$('hidePromo').checked = true; $('hideTerm').checked = true; savePromo(); applyFilters()")
        pg.wait_for_timeout(400)
        check(f"{tag} setArea filters the list", pg.evaluate("curList.length") < len(before))
        check(f"{tag} alert filters saved on the server",
              get_json(base, "/notify/status").get("filters", {}).get("hide_promo") is True,
              get_json(base, "/notify/status"))
        pg.evaluate("resetFilters()")
        pg.wait_for_timeout(600)
        st = get_json(base, "/notify/status").get("filters", {})
        check(f"{tag} reset also clears the server alert filters",
              st.get("hide_promo") is False and st.get("hide_fixed_term") is False, st)
        check(f"{tag} reset clears the area",
              pg.evaluate("area === null && areaLayer.getLayers().length === 0") and pg.evaluate("curList.length") == len(before)
              and pg.evaluate("localStorage.getItem('wgp_area')") is None)
    check(f"{tag} draw-area page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()


def run_top5(pages: Pages, base: str, w: int, h: int):
    """Top 5 is ranked on the server; ticking a hide filter or drawing an area must change it."""
    tag = str(w)
    pg = pages.new(w, h, touch=w <= 760)

    def top5() -> list[int]:
        pg.evaluate("closeBest(); showBest()")
        pg.wait_for_function("!document.querySelector('#best-list #best-loading')", timeout=10000)
        return pg.evaluate("[...document.querySelectorAll('#best-list .best-entry')].map(e => +e.innerHTML.match(/selectListing\\((\\d+)\\)/)[1])")

    with step(f"{tag} top 5 filters", pg, pages):
        wait_list(pg, base)
        pg.evaluate("resetFilters()")
        pg.wait_for_timeout(300)
        base5 = top5()
        check(f"{tag} Top 5 unfiltered: 5 entries, no filter note",
              len(base5) == 5 and pg.locator("#best-list .best-filters").count() == 0, base5)
        check(f"{tag} (seed) unfiltered Top 5 contains a befristet listing",
              pg.evaluate(f"{json.dumps(base5)}.some(id => isFixedTerm(byId[id]))"))
        pg.evaluate("closeBest(); $('hideTerm').checked = true; savePromo(); applyFilters()")
        term5 = top5()
        check(f"{tag} ticking 'Hide befristet' changes Top 5 and drops befristet",
              term5 != base5 and len(term5) == 5 and pg.evaluate(f"{json.dumps(term5)}.every(id => !isFixedTerm(byId[id]))"),
              f"{base5} -> {term5}")
        pg.evaluate("closeBest(); $('hidePromo').checked = true; savePromo(); applyFilters()")
        both5 = top5()
        check(f"{tag} with 'Hide priority/premium' too: no active promo, note names both filters",
              pg.evaluate(f"{json.dumps(both5)}.every(id => !promoActive(byId[id]) && !isFixedTerm(byId[id]))")
              and "no priority/premium" in pg.text_content("#best-list .best-filters")
              and "no befristet" in pg.text_content("#best-list .best-filters"))
        pages.shot(pg, f"{tag}-13a-top5-filtered")
        pg.evaluate("closeBest(); resetFilters()")
        pg.wait_for_timeout(300)
        check(f"{tag} reset brings the unfiltered Top 5 back", top5() == base5)
        pg.evaluate("closeBest(); setArea(" + json.dumps([[47.37, 8.53], [47.37, 8.55], [47.39, 8.55], [47.39, 8.53]]) + ")")
        pg.wait_for_timeout(300)
        area5 = top5()
        check(f"{tag} drawing an area limits Top 5 to listings inside it",
              area5 and area5 != base5 and pg.evaluate(f"{json.dumps(area5)}.every(id => inArea(byId[id]))")
              and "in drawn area" in pg.text_content("#best-list .best-filters"), f"{base5} -> {area5}")
        pages.shot(pg, f"{tag}-13b-top5-area")
        # the rent slider is a view filter and deliberately leaves Top 5 alone
        pg.evaluate("closeBest(); clearArea(true)")
        pg.evaluate("$('maxRent').value = $('maxRent').min; applyFilters()")
        pg.wait_for_timeout(300)
        check(f"{tag} rent slider does not change Top 5", top5() == base5)
        pg.evaluate("closeBest(); resetFilters()")
        pg.wait_for_timeout(600)
    check(f"{tag} top 5 page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
    pg.context.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Opt-in browser smoke test of the WGPendler UI.")
    ap.add_argument("--out", help="screenshot folder (default: <system temp>/wgp-ui-smoke/<timestamp>)")
    a = ap.parse_args(argv)
    if os.environ.get("RUN_UI_SMOKE") != "1":
        print("skipped (set RUN_UI_SMOKE=1 to run)")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is missing: pip install playwright")
        return 2

    out = Path(a.out).resolve() if a.out else (
        Path(tempfile.gettempdir()) / "wgp-ui-smoke" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    if out == ROOT or ROOT in out.parents:
        print(f"refusing to write screenshots inside the repo ({out}); pick a folder outside it")
        return 2
    out.mkdir(parents=True, exist_ok=True)

    local_cdn = None
    if os.environ.get("WGP_LEAFLET_DIR") or not cdn_reachable():
        d = leaflet_dir()
        d.mkdir(parents=True, exist_ok=True)
        if not prepare_local_leaflet(d):
            print(f"unpkg.com is unreachable and there is no local Leaflet copy in {d}.\n"
                  f"Fill it once:  cd {d} && npm pack leaflet@1.9.4 leaflet.markercluster@1.5.3")
            return 2
        local_cdn = d.resolve()
        print(f"[ui] serving Leaflet from {local_cdn}")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    work = Path(tempfile.mkdtemp(prefix="wgp_ui_smoke_"))
    log_path = out / "server.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "dev_server.py"), "--port", str(port), "--work", str(work),
         "--fake-llm", "--outcomes", "--group", "--clear-template"],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                get_json(base, "/ingest/status")
                break
            except Exception:
                time.sleep(0.5)
        if not check("dev server boots", proc.poll() is None):
            print(log_path.read_text()[-2000:])
            return 1
        api = {"solo": get_json(base, "/listings?kind=wg_room,studio")}
        group = get_json(base, "/group/listings")
        gids = {l["id"] for l in group["listings"]}
        allrows = get_json(base, "/listings")
        api["group_draft"] = next((l["id"] for l in allrows
                                   if l["id"] in gids and not l.get("sent_at") and not l.get("last_sent_at")), None)
        api["group_followup"] = next((l["id"] for l in allrows
                                      if l["id"] in gids and l.get("followup_due")), None)
        api["photo_listing"] = None
        for l in api["solo"][:40]:   # photos come only with /listings/{id}, as in the detail view
            ph = get_json(base, f"/listings/{l['id']}").get("photos") or []
            if len(ph) >= 4:
                api["photo_listing"] = (l["id"], len(ph))
                break
        print(f"[ui] {base}: {len(api['solo'])} solo listings, {len(gids)} group flats; screenshots -> {out}")

        with sync_playwright() as p:
            browser = launch_browser(p)
            pages = Pages(browser, base, local_cdn, out)
            for w, h in SIZES:
                run_size(pages, base, w, h, api)
                run_area(pages, base, w, h)
                run_top5(pages, base, w, h)
                run_a11y(pages, base, w, h)
                run_photos(pages, base, w, h, api)
            browser.close()
        if pages.external:
            print(f"[ui] blocked external hosts (answered offline): {sorted(pages.external)}")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)
    srv = log_path.read_text()
    check("server log has no tracebacks", "Traceback" not in srv, srv[-500:])
    print(f"\nscreenshots: {out}")
    print(f"{passes} passed, {len(failures)} failed" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
