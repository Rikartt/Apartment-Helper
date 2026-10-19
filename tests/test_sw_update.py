"""A code update (git pull + restart) must reach an installed PWA without clearing site data.

Before: frontend files had no Cache-Control (browsers cache them heuristically for ~10% of the
file's age, so the worker's network-first fetch got the old copy from the HTTP cache) and sw.js
never changed, so the shell cache was never replaced. See backend/frontend.py.
"""
import re
from pathlib import Path

from backend.frontend import BUILD_PLACEHOLDER, FrontendFiles, build_id

FRONT = Path(__file__).resolve().parent.parent / "frontend"


def test_frontend_files_always_revalidate(client):
    for path in ("/", "/index.html", "/stats.html", "/pwa.js", "/geo.js", "/checklist.js",
                 "/manifest.json", "/icons/icon-192.png"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path


def test_unchanged_file_revalidates_with_304(client):
    r = client.get("/geo.js")
    r2 = client.get("/geo.js", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
    assert r2.headers.get("cache-control") == "no-cache"


def test_sw_carries_build_id(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "javascript" in r.headers["content-type"]
    assert BUILD_PLACEHOLDER not in r.text
    m = re.search(r"const BUILD = '([0-9a-f]{12})';", r.text)
    assert m and m.group(1) == build_id(FRONT)


def test_sw_gzip_still_applies(client):
    r = client.get("/sw.js", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_build_id_changes_with_any_frontend_file(tmp_path):
    (tmp_path / "index.html").write_text("<h1>a</h1>")
    (tmp_path / "geo.js").write_text("x")
    (tmp_path / "sw.js").write_text("const BUILD = '__WGP_BUILD__';")
    a = build_id(tmp_path)
    assert a == build_id(tmp_path)                       # stable across restarts
    (tmp_path / "geo.js").write_text("y")
    b = build_id(tmp_path)
    assert b != a
    (tmp_path / "new.js").write_text("")
    assert build_id(tmp_path) != b
    sw = FrontendFiles(directory=tmp_path, html=True)._sw
    assert build_id(tmp_path) in sw and BUILD_PLACEHOLDER not in sw


def test_sw_bypasses_http_cache_for_the_shell():
    sw = (FRONT / "sw.js").read_text()
    assert "const SHELL = 'wgp-shell-' + BUILD" in sw
    assert "cache: 'reload'" in sw            # precache never stores an old HTTP-cache copy
    assert "fetch(req, { cache: 'no-cache' })" in sw   # network-first really asks the server
    assert "self.skipWaiting()" in sw and "self.clients.claim()" in sw


def test_pwa_offers_reload_after_update():
    js = (FRONT / "pwa.js").read_text()
    assert "controllerchange" in js and "sw-update" in js
    assert "location.reload()" in js
    assert "reg.update()" in js
