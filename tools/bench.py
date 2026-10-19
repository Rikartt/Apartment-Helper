"""Realistic-size performance check: times the hot endpoints on a big synthetic DB.
NOT run by pytest. Offline: synthetic DB, fake LLM, zero network or Anthropic calls.

    python tools/bench.py                          # 4000 listings, 300 contacted, API timings
    python tools/bench.py --n 5000 --contacted 400 --repeat 7
    python tools/bench.py --browser                # also page load at 1440x900 and 390x844
    python tools/bench.py --db /tmp/big.db         # reuse a DB built earlier (seeded if missing)

API part (in-process, FastAPI TestClient against the same app the dev server runs):
  cold = first call after dropping the in-memory result caches (trust, Top 5, learned model),
  i.e. the first request after an ingest or extraction changed the data; per-text memos are
  kept, except in the "fresh process" row (a server restart before the startup warm-up).
  warm = median of --repeat further calls. Covers /listings (as the UI asks for it, and
  the full list), /group/listings, /best (Top 5, fake LLM), /stats, the single-listing
  endpoints (detail, status change), notify.pending_candidates and trust.annotate. Sizes are
  the decoded JSON and, after "wire", what went over HTTP (the client sends
  Accept-Encoding: gzip like a browser).

Browser part (--browser; needs Playwright + Chromium, Leaflet from the local copy that
tests/ui_smoke.py uses): starts tools/dev_server.py on the same DB and measures, per viewport,
time from navigation to the first card, to the full list state (header count + every pin in
the marker cluster), plus one in-page applyFilters() re-render and the "Message today" picks.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for p in (ROOT, TOOLS, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def build_db(db: Path, n: int, contacted: int, seed: int, work: Path) -> None:
    if db.exists():
        print(f"[bench] reusing {db}")
        return
    import dev_seed
    from dev_server import SAMPLE_GROUP
    profile = json.loads((ROOT / "profile.json").read_text())
    profile["group"] = json.loads(json.dumps(SAMPLE_GROUP))
    t0 = time.perf_counter()
    info = dev_seed.build(db, n=n, seed=seed, outcomes=True, contacted=contacted, profile=profile)
    print(f"[bench] seeded {info['listings']} listings, outcomes {info['outcomes']} "
          f"in {_ms(t0) / 1000:.1f}s")


def api_bench(db: Path, work: Path, repeat: int) -> list[tuple[str, float, float, str]]:
    import dev_server
    m = dev_server.prepare(work, db, clear_template=False, group=True, outcomes=True,
                           fake_llm=True, seed=7, n=0)
    from fastapi.testclient import TestClient
    from backend import db as dbm, learning, notify, trust
    dbm.init_db()   # what the lifespan would do (the scheduler thread is not started)
    client = TestClient(m.app)

    def drop_caches():
        trust._cache["entry"] = None
        m._best_cache.clear()
        learning._cache.clear()
        learning._attempted.clear()

    def http(path):
        def f():
            r = client.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
            return r
        return f

    universe = dbm.get_all_listings(True)
    one = universe[len(universe) // 2]["id"]
    quiet = next((l["id"] for l in universe if not l.get("status")), one)

    def post(path, body):
        def f():
            r = client.post(path, json=body)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
            return r
        return f

    cases = [
        ("GET /listings?kind=wg_room,studio", http("/listings?kind=wg_room,studio")),
        ("GET /listings (all)", http("/listings")),
        ("GET /group/listings", http("/group/listings")),
        ("GET /best (Top 5, fake LLM)", http("/best")),
        ("GET /stats", http("/stats")),
        ("GET /listings/{id} (detail)", http(f"/listings/{one}")),
        ("POST /listings/{id}/status", post(f"/listings/{quiet}/status", {"status": None})),
        ("notify.pending_candidates()", lambda: notify.pending_candidates(now=None)),
        ("trust.annotate(all rows)", lambda: trust.annotate([dict(r) for r in universe])),
        ("trust.annotate, fresh process", "fresh"),
    ]
    # the stats page trains the learned model once; afterwards every score blends it in,
    # which is the realistic steady state with a few hundred outcomes
    client.get("/stats")
    trust.annotate([dict(r) for r in universe])   # fill the per-text memos (see "cold" above)
    out = []
    for name, fn in cases:
        drop_caches()
        if fn == "fresh":   # also forget the per-text memos (what a server restart sees)
            for attr in ("_signals_cache", "_sketch_cache"):
                getattr(trust, attr, {}).clear()
            fn = cases[-2][1]
        learning.current_model()   # model load is per process, not per request
        t0 = time.perf_counter()
        res = fn()
        cold = _ms(t0)
        warm = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            warm.append(_ms(t0))
        size = ""
        if hasattr(res, "content"):
            n, wire = len(res.content), res.num_bytes_downloaded
            size = (f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.1f} kB") + \
                (f" (wire {wire / 1e3:.0f} kB)" if wire != n else "")
        elif isinstance(res, list):
            size = f"{len(res)} rows"
        out.append((name, cold, statistics.median(warm), size))
        print(f"  {name:38s} cold {cold:8.0f} ms   warm {statistics.median(warm):8.0f} ms   {size}",
              flush=True)
    return out


def browser_bench(db: Path, work: Path) -> list[tuple[str, float, float, float, float, str]]:
    import ui_smoke as us
    from playwright.sync_api import sync_playwright
    local = None
    if not us.cdn_reachable():
        d = us.leaflet_dir()
        if not us.prepare_local_leaflet(d):
            raise SystemExit(f"Leaflet not reachable and no local copy in {d} (see tests/ui_smoke.py)")
        local = d
    port = us.free_port()
    base = f"http://127.0.0.1:{port}"
    log = open(work / "server.log", "w")
    proc = subprocess.Popen([sys.executable, str(TOOLS / "dev_server.py"), "--port", str(port),
                             "--work", str(work / "srv"), "--db", str(db), "--fake-llm", "--group"],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    out = []
    try:
        for _ in range(240):
            try:
                us.get_json(base, "/ingest/status")
                break
            except Exception:
                time.sleep(0.25)
        us.get_json(base, "/listings?kind=wg_room,studio")   # warm the server-side trust cache
        with sync_playwright() as p:
            browser = us.launch_browser(p)
            pages = us.Pages(browser, base, local, work)
            for w, h in us.SIZES:
                pg = pages.new(w, h)
                t0 = time.perf_counter()
                pg.goto(base + "/", wait_until="domcontentloaded")
                pg.wait_for_selector(".card", timeout=60000)
                first = _ms(t0)
                pg.wait_for_function("loadState === 'ok' && /\\d+ of \\d+/.test($('count').textContent) "
                                     "&& cluster.getLayers().length >= curList.length", timeout=60000)
                full = _ms(t0)
                pins = pg.evaluate("cluster.getLayers().length")
                refilter = pg.evaluate("""() => new Promise(res => {
                    const t = performance.now(); applyFilters();
                    requestAnimationFrame(() => res(performance.now() - t)); })""")
                today = pg.evaluate("""() => { const t = performance.now();
                    for (let i = 0; i < 5; i++) renderToday(); return (performance.now() - t) / 5; }""")
                out.append((f"{w}x{h}", first, full, refilter, today, f"{pins} pins"))
                print(f"  {w}x{h}: first card {first:6.0f} ms  list+all pins {full:6.0f} ms  "
                      f"applyFilters {refilter:5.0f} ms  Message today {today:5.1f} ms  ({pins} pins)",
                      flush=True)
                if pg.errs:
                    print(f"  [browser errors] {pg.errs[:3]}")
                pg.context.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(10)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=4000, help="listings (default 4000)")
    ap.add_argument("--contacted", type=int, default=300, help="contacted listings (default 300)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", help="DB to use (seeded if missing; default: a temp file)")
    ap.add_argument("--repeat", type=int, default=5, help="warm calls per case (median)")
    ap.add_argument("--browser", action="store_true", help="also measure page load in Chromium")
    a = ap.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="wgp_bench_"))
    db = Path(a.db).resolve() if a.db else work / "bench.db"
    if db == (ROOT / "wgpendler.db").resolve():
        ap.error("refusing to use the real wgpendler.db")
    build_db(db, a.n, a.contacted, a.seed, work)
    print(f"[bench] API ({a.repeat} warm calls each)")
    api_bench(db, work, a.repeat)
    if a.browser:
        print("[bench] browser")
        browser_bench(db, work)
    print(f"[bench] work dir {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
