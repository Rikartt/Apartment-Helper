import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend import backup


@pytest.fixture
def bdir(db, tmp_path, monkeypatch):
    d = tmp_path / "bk"
    monkeypatch.setenv("WGP_BACKUP_DIR", str(d))
    return d


def test_backup_copies_and_prunes(db, bdir, monkeypatch):
    monkeypatch.setenv("WGP_BACKUP_KEEP", "3")
    day = date(2026, 10, 1)
    for i in range(5):
        backup.run_backup(day + timedelta(days=i))
    files = sorted(p.name for p in bdir.iterdir())
    assert files == [f"wgpendler-2026-10-0{n}.db" for n in (3, 4, 5)]
    c = sqlite3.connect(bdir / files[-1])
    assert c.execute("select count(*) from sqlite_master").fetchone()[0] > 0
    c.close()


def test_ensure_today_only_once(db, bdir):
    assert backup.ensure_today() is not None
    assert backup.ensure_today() is None
    assert len(backup.list_backups()) == 1


def test_endpoints(db, bdir):
    from backend.main import app
    c = TestClient(app)
    s = c.get("/backup/status").json()
    assert s["count"] == 0 and not s["today_done"]
    r = c.post("/backup/now")
    assert r.status_code == 200 and r.json()["today_done"]
    assert c.get("/backup/status").json()["count"] == 1
