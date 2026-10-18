import json
from pathlib import Path

FRONT = Path(__file__).resolve().parent.parent / "frontend"


def test_manifest_and_icons_served(client):
    r = client.get("/manifest.json")
    assert r.status_code == 200
    m = r.json()
    assert m["name"] and m["short_name"] and m["display"] == "standalone"
    assert m["theme_color"] == "#111111"
    for icon in m["icons"]:
        ir = client.get(icon["src"])
        assert ir.status_code == 200
        assert ir.content[:8] == b"\x89PNG\r\n\x1a\n"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_service_worker_served_no_cache(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "network only, never cached" in r.text


def test_index_links_manifest(client):
    html = client.get("/").text
    assert 'rel="manifest"' in html and "/pwa.js" in html


def test_sw_never_caches_api():
    sw = (FRONT / "sw.js").read_text()
    # the shell list must not include data endpoints
    assert "/listings" not in sw.split("SHELL_URLS = [")[1].split("]")[0]


# ---- access token guard ----
def test_guard_off_by_default(client, monkeypatch):
    monkeypatch.delenv("WGP_ACCESS_TOKEN", raising=False)
    assert client.get("/listings").status_code == 200


def test_guard_blocks_without_token(client, monkeypatch):
    monkeypatch.setenv("WGP_ACCESS_TOKEN", "s3cret")
    assert client.get("/listings").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/listings?token=wrong").status_code == 401
    # manifest/icons stay open so the install prompt works
    assert client.get("/manifest.json").status_code == 200


def test_guard_token_sets_cookie_then_allows(client, monkeypatch):
    monkeypatch.setenv("WGP_ACCESS_TOKEN", "s3cret")
    r = client.get("/?token=s3cret&x=1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?x=1"
    assert "wgp_access" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert "s3cret" not in r.headers["set-cookie"]
    # cookie jar now carries it
    assert client.get("/listings").status_code == 200
    assert client.get("/").status_code == 200


def test_guard_rejects_forged_cookie(client, monkeypatch):
    monkeypatch.setenv("WGP_ACCESS_TOKEN", "s3cret")
    client.cookies.set("wgp_access", "s3cret")
    assert client.get("/listings").status_code == 401
