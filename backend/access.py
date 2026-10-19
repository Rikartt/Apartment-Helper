"""Optional shared-secret guard for LAN/phone access.

If WGP_ACCESS_TOKEN is set, every request must carry it: open http://<ip>:8000/?token=SECRET once,
the server sets a cookie and redirects to the clean URL. Unset (the default) = no guard.
This is a simple guard for a home network, not real authentication: use HTTPS or a VPN
if you expose the app beyond your own Wi-Fi.
"""
import hashlib
import hmac
import os
from urllib.parse import urlencode

from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.requests import Request

COOKIE = "wgp_access"
# Public, non-sensitive static files the browser fetches without cookies (install prompt).
OPEN_PREFIXES = ("/manifest.json", "/icons/")


def _token() -> str:
    return os.environ.get("WGP_ACCESS_TOKEN", "").strip()


def _cookie_value(token: str) -> str:
    return hashlib.sha256(("wgp-access:" + token).encode()).hexdigest()


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def access_guard(request: Request, call_next):
    token = _token()
    if not token:
        response = await call_next(request)
    elif request.url.path.startswith(OPEN_PREFIXES):
        response = await call_next(request)
    else:
        expected = _cookie_value(token)
        if _eq(request.cookies.get(COOKIE, ""), expected):
            response = await call_next(request)
        else:
            supplied = request.query_params.get("token", "")
            if supplied and _eq(supplied, token):
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
                # "//host" would be read as a protocol-relative URL: keep the redirect on this origin
                target = "/" + request.url.path.lstrip("/") + ("?" + urlencode(rest) if rest else "")
                response = RedirectResponse(target, status_code=303)
                response.set_cookie(COOKIE, expected, max_age=365 * 86400, httponly=True, samesite="lax")
            else:
                response = PlainTextResponse(
                    "WGPendler: access token required. Open /?token=<WGP_ACCESS_TOKEN> once.",
                    status_code=401)
    if request.url.path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache"   # always revalidate the worker
    return response
