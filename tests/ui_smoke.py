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
  reply stats page (and the menu link to it)             group mode follow-up draft button
  no console errors on any page

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
        self.external.add(host)
        if route.request.resource_type == "image":   # map tiles
            return route.fulfill(status=200, body=BLANK_PNG, content_type="image/png")
        return route.abort()

    def new(self, w: int, h: int, tour_done: bool = True):
        ctx = self.browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                                       service_workers="block")
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
        check(f"{tag} walkthrough hidden once done", not pg.is_visible("#hint"))
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
        # give closeDetail's deferred setCursor/zoomToShowLayer time to finish (a real user is
        # slower than this anyway): hiding the pin within ~50 ms leaves markercluster with a
        # dangling moveend listener that breaks every later selectListing
        pg.wait_for_timeout(500)
        pg.evaluate("$('showLow').checked = false; applyFilters()")
        pg.wait_for_timeout(300)

    with step(f"{tag} priority filter and Message today", pg, pages):
        check(f"{tag} seed has priority/premium listings", pg.evaluate("allListings.some(l => l.promo)"))
        pg.evaluate("setShow('sent')")
        pg.wait_for_timeout(300)
        check(f"{tag} Message today stays visible outside the To do view", pg.evaluate("!$('today').hidden"))
        pg.evaluate("setShow('todo')")
        pg.evaluate("$('hidePromo').checked = true; savePromo(); applyFilters()")
        pg.wait_for_timeout(400)
        check(f"{tag} 'Hide priority/premium' removes them from the list",
              pg.evaluate("curList.length > 0 && !curList.some(l => l.promo)"))
        check(f"{tag} ...and from Message today",
              pg.evaluate("!$('today').hidden && ![...document.querySelectorAll('.today-row')].some(r => byId[+r.dataset.id].promo)"))
        pg.evaluate("$('hidePromo').checked = false; savePromo(); applyFilters()")
        pg.wait_for_timeout(300)

    with step(f"{tag} detail", pg, pages):
        first = pg.locator(".card").first
        title = pg.evaluate("byId[+document.querySelector('.card').dataset.id].title")
        first.click()
        pg.wait_for_function("$('detail-view').classList.contains('open')", timeout=5000)
        check(f"{tag} clicking a card opens the detail view", is_open(pg))
        check(f"{tag} detail shows the listing title", title.split()[0] in pg.text_content("#detail-view"), title)
        pages.shot(pg, f"{tag}-04-detail")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        check(f"{tag} Esc closes the detail", not is_open(pg))

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
        pages.shot(pg, f"{tag}-10-group-list")
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
        pg.evaluate("$('draft-box').scrollIntoView({block:'nearest'})")
        pages.shot(pg, f"{tag}-11-group-followup")
    check(f"{tag} group page: no console errors", not pg.errs, "; ".join(pg.errs[:3]))
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
        api["group_followup"] = next((l["id"] for l in allrows
                                      if l["id"] in gids and l.get("followup_due")), None)
        print(f"[ui] {base}: {len(api['solo'])} solo listings, {len(gids)} group flats; screenshots -> {out}")

        with sync_playwright() as p:
            browser = launch_browser(p)
            pages = Pages(browser, base, local_cdn, out)
            for w, h in SIZES:
                run_size(pages, base, w, h, api)
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
