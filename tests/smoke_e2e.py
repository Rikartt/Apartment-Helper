"""Opt-in end-to-end smoke. NOT collected by pytest (name is not test_*).

    RUN_E2E=1 .venv/bin/python tests/smoke_e2e.py

Boots the real server on port 8102 with a throwaway DB and a copy of profile.json, waits
for the startup ingest to finish, hits endpoints, and loads the page in Playwright
checking for console errors. Does live scraping (polite, same as the app's own startup).

Anthropic: /extract, /best (analysis) and /draft are SKIPPED unless WGP_LIVE_API=1.
The server is started WITHOUT ANTHROPIC_API_KEY unless WGP_LIVE_API=1, so no API call
can happen (startup never extracts either).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8102
BASE = f"http://127.0.0.1:{PORT}"
LIVE = os.environ.get("WGP_LIVE_API") == "1"
INGEST_WAIT = int(os.environ.get("E2E_INGEST_WAIT", "240"))

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def http(method, path, body=None, timeout=60):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    if os.environ.get("RUN_E2E") != "1":
        print("skipped (set RUN_E2E=1 to run)")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="wgp_e2e_"))
    # run a copy of the app tree so the real DB / profile.json are untouched
    work = tmp / "app"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.db", "tests", ".claude"))
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    if LIVE and os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    log = open(tmp / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(PORT)],
                            cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if http("GET", "/listings", timeout=2)[0] == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            check("server boots", False)
            return 1
        check("server boots", True)

        # wait for ingest: log shows adapters done or listings stabilise
        t0, last, stable = time.time(), -1, 0
        while time.time() - t0 < INGEST_WAIT:
            n = len(json.loads(http("GET", "/listings")[1]))
            stable = stable + 1 if n == last and n > 0 else 0
            last = n
            if stable >= 6:
                break
            time.sleep(5)
        print(f"ingested {last} listings")
        check("ingest produced listings", last > 0)

        s, b = http("GET", "/listings")
        rows = json.loads(b)
        check("GET /listings", s == 200 and isinstance(rows, list))
        need = {"id", "source", "title", "lat", "lng", "rent_chf", "fit_score", "reply_score", "url"}
        check("listing fields", all(need <= set(r) for r in rows))
        check("coords in Switzerland", all(45.5 < r["lat"] < 48 and 5.5 < r["lng"] < 10.6 for r in rows))
        check("scores in range", all(0 <= (r["fit_score"] or 0) <= 100 and 0 <= (r["reply_score"] or 0) <= 100 for r in rows))

        s, b = http("GET", "/profile")
        prof = json.loads(b)
        check("GET /profile", s == 200 and isinstance(prof, dict) and prof)
        s, b = http("PUT", "/profile", prof)
        check("PUT /profile", s == 200 and json.loads(b).get("ok") is True)
        s, _ = http("POST", "/ingest?sources=mock")
        check("POST /ingest?sources=mock", s == 200)
        check("GET / serves page", http("GET", "/")[0] == 200)

        if LIVE:
            s, b = http("POST", "/extract", timeout=300)
            check("POST /extract (live)", s == 200)
            s, b = http("GET", "/best", timeout=120)
            check("GET /best (live)", s == 200)
            if rows:
                s, _ = http("POST", f"/draft/{rows[0]['id']}", timeout=120)
                check("POST /draft (live)", s == 200)
        else:
            print("SKIP /extract, /draft, /best analysis (set WGP_LIVE_API=1 to spend API credit)")
            s, b = http("GET", "/best")  # no key -> no Claude call
            check("GET /best (no key, no API)", s == 200 and isinstance(json.loads(b), list))
            s, _ = http("POST", f"/draft/{rows[0]['id']}") if rows else (503, "")
            check("POST /draft without key -> 503", s == 503)

        # browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("SKIP browser check (playwright missing)")
        else:
            errors = []
            with sync_playwright() as p:
                br = p.chromium.launch()
                page = br.new_page()
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.goto(BASE, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(2000)
                check("page has map container", page.locator(".leaflet-container").count() > 0)
                check("markers rendered", page.locator(".leaflet-marker-icon, .leaflet-interactive").count() > 0)
                br.close()
            # tile CDN failures are network noise, not app bugs
            real = [e for e in errors if "tile" not in e.lower() and "ERR_" not in e]
            check("no console errors", not real, "; ".join(real[:5]))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        log.close()
        print(f"server log: {tmp / 'server.log'}")
    print(f"\n{len(failures)} failure(s)" if failures else "\nall smoke checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
