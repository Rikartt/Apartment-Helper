"""Daily SQLite backups via the sqlite3 backup API (safe while the app runs in WAL mode).

Files: backups/wgpendler-YYYY-MM-DD.db (git-ignored), newest WGP_BACKUP_KEEP (default 7) kept.
WGP_BACKUP_DIR overrides the folder. Statuses and sent messages are personal history.
"""
import os
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import db as dbm

router = APIRouter()
DEFAULT_DIR = Path(__file__).parent.parent / "backups"
_PAT = re.compile(r"^wgpendler-(\d{4}-\d{2}-\d{2})\.db$")
_lock = threading.Lock()
_last_error: str | None = None


def backup_dir() -> Path:
    v = os.environ.get("WGP_BACKUP_DIR", "").strip()
    return Path(v) if v else DEFAULT_DIR


def backup_keep() -> int:
    try:
        return max(1, int(os.environ.get("WGP_BACKUP_KEEP", 7)))
    except ValueError:
        return 7


def list_backups(directory: Path | None = None) -> list[dict]:
    d = directory or backup_dir()
    out = []
    if d.is_dir():
        for p in d.iterdir():
            m = _PAT.match(p.name)
            if m:
                out.append({"file": p.name, "date": m.group(1), "bytes": p.stat().st_size})
    return sorted(out, key=lambda b: b["date"], reverse=True)


def prune(directory: Path, keep: int) -> list[str]:
    removed = []
    for b in list_backups(directory)[keep:]:
        try:
            (directory / b["file"]).unlink()
            removed.append(b["file"])
        except OSError:
            pass
    return removed


def run_backup(today: date | None = None) -> dict:
    """Write today's backup (replacing one from earlier today), then prune. Raises on failure."""
    global _last_error
    today = today or date.today()
    d = backup_dir()
    with _lock:
        try:
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"wgpendler-{today.isoformat()}.db"
            tmp = d / f".{target.name}.tmp"
            tmp.unlink(missing_ok=True)
            src = sqlite3.connect(dbm.DB_PATH, timeout=30)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            os.replace(tmp, target)
            removed = prune(d, backup_keep())
            _last_error = None
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"[:300]
            raise
    return {"file": target.name, "bytes": target.stat().st_size, "pruned": removed}


def ensure_today(today: date | None = None) -> dict | None:
    """Back up unless today's file exists. Never raises (used by the background thread)."""
    today = today or date.today()
    if (backup_dir() / f"wgpendler-{today.isoformat()}.db").exists():
        return None
    try:
        return run_backup(today)
    except Exception as e:
        print(f"[backup] failed: {e}")
        return None


def status() -> dict:
    d = backup_dir()
    items = list_backups(d)
    today = date.today().isoformat()
    return {"dir": str(d), "keep": backup_keep(), "count": len(items),
            "latest": items[0] if items else None,
            "today_done": bool(items and items[0]["date"] == today),
            "backups": items, "last_error": _last_error}


@router.get("/backup/status")
def backup_status():
    return JSONResponse(status())


@router.post("/backup/now")
def backup_now():
    try:
        r = run_backup()
    except Exception as e:
        return JSONResponse({"error": f"backup failed: {e}"}, status_code=500)
    return JSONResponse({**r, **status()})
