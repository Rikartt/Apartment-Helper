"""Serves frontend/ so a code update (git pull + restart) reaches installed PWAs without clearing site data.

Two things made a phone or MacBook keep showing the old UI:
- StaticFiles sends no Cache-Control, so the browser applies heuristic freshness (10% of the file's
  age: a file untouched for 30 days counts as fresh for 3 days) and the service worker's
  network-first fetch was answered from the HTTP cache without asking the server.
- sw.js never changed between releases, so the browser never saw an update and the shell cache
  kept whatever it had.

Now every frontend file is sent with `Cache-Control: no-cache` (the browser revalidates with the
ETag; unchanged files cost a 304), and sw.js carries a build id: a hash of all frontend files taken
at server start. A changed file means a new sw.js, which the browser installs; pwa.js then offers a
reload to pages that are still open.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

BUILD_PLACEHOLDER = "__WGP_BUILD__"


def build_id(directory: Path) -> str:
    """Short hash of every file under `directory` (paths and contents), stable across restarts."""
    h = hashlib.sha256()
    for f in sorted(p for p in Path(directory).rglob("*") if p.is_file()):
        h.update(f.relative_to(directory).as_posix().encode() + b"\0")
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


class FrontendFiles(StaticFiles):
    def __init__(self, *, directory: Path, **kw):
        super().__init__(directory=directory, **kw)
        self.build = build_id(directory)
        self._sw = (Path(directory) / "sw.js").read_text(encoding="utf-8").replace(BUILD_PLACEHOLDER, self.build)

    async def get_response(self, path: str, scope):
        if path == "sw.js":
            return Response(self._sw, media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = "no-cache"
        return response
