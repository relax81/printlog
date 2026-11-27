"""Komplettsicherung als ZIP: Export, Prüfung beim Einspielen, Zurücksetzen.

Aufruf: python -m pytest tests
"""

import ast
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import archive  # noqa: E402
from app.db import Store  # noqa: E402
from app.i18n_en import EN  # noqa: E402


def _setup():
    data = Path(tempfile.mkdtemp())
    images = data / "images"
    images.mkdir()
    store = Store(data / "printlog.db")
    cid = store.save_customer(None, "Testkunde", None, None)
    now = time.time()
    job = store.insert(printer="p1", task_name="Halter", state="done", status="fertig", start_ts=now - 3600,
                       end_ts=now, cost_total=1.5, customer_id=cid, image="job_1.jpg", created_at=now)
    (images / "job_1.jpg").write_bytes(b"\xff\xd8jpeg")
    (images / "bon_logo_1.png").write_bytes(b"\x89PNGlogo")
    store.set_setting("bon_logo", "bon_logo_1.png")
    store.set_setting("margin1_pct", "20")
    store.set_setting("language", "de")
    return data, images, store, job


def _export(store, images, data) -> Path:
    z = data / "export.zip"
    archive.build_zip(store, images, z)
    return z


def test_roundtrip() -> None:
    data, images, store, job = _setup()
    z = _export(store, images, data)
    names = set(zipfile.ZipFile(z).namelist())
    assert {"printlog.db", "manifest.json", "einstellungen.json", "images/job_1.jpg", "images/bon_logo_1.png"} <= names

    # Danach ändert sich alles: neuer Druck, anderes Bild, andere Einstellung
    store.insert(printer="p1", task_name="Neu", state="done", status="fertig", created_at=time.time())
    (images / "extra.jpg").write_bytes(b"x")
    store.set_setting("margin1_pct", "50")
    store.set_setting("language", "en")

    token = archive.new_token()
    info = archive.unpack(z, archive.stage_dir(data) / token)
    assert info["jobs"] == 1 and info["customers"] == 1 and info["images"] == 2 and not info["missing"]
    res = archive.apply(store, images, data, token)
    assert res["jobs"] == 1
    assert store.get(job)["task_name"] == "Halter"
    cfg = store.settings()
    assert cfg["margin1_pct"] == "20" and cfg["bon_logo"] == "bon_logo_1.png"
    assert cfg["language"] == "en"  # Sprache bleibt wie eingestellt
    assert sorted(p.name for p in images.iterdir()) == ["bon_logo_1.png", "job_1.jpg"]
    # Vorher wurde automatisch gesichert, mit dem Stand vor dem Einspielen (2 Drucke)
    safety = archive.safety_path(data, res["safety"])
    assert safety is not None
    info2 = archive.unpack(safety, archive.stage_dir(data) / archive.new_token())
    assert info2["jobs"] == 2


def _zip_with(data: Path, entries: dict[str, bytes], symlink: str | None = None) -> Path:
    z = data / f"bad-{time.time_ns()}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name, raw in entries.items():
            zf.writestr(name, raw)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return z


def _fresh_db_bytes(trigger: bool = False) -> bytes:
    d = Path(tempfile.mkdtemp())
    s = Store(d / "x.db")
    if trigger:
        s._conn.execute("CREATE TRIGGER t AFTER INSERT ON jobs BEGIN DELETE FROM customers; END")
        s._conn.commit()
    s._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    p = d / "copy.db"
    s.backup_to(p)
    return p.read_bytes()


@pytest.mark.parametrize("name", ["../printlog.db", "images/../../evil.jpg", "/etc/printlog.db",
                                  "images\\evil.jpg", "C:/x/printlog.db", "images/sub/../x.jpg"])
def test_reject_paths(name: str) -> None:
    data = Path(tempfile.mkdtemp())
    z = _zip_with(data, {"printlog.db": _fresh_db_bytes(), name: b"x"})
    with pytest.raises(archive.ArchiveError):
        archive.unpack(z, data / "out")
    assert not (data.parent / "evil.jpg").exists()


def test_reject_unknown_and_symlink() -> None:
    data = Path(tempfile.mkdtemp())
    with pytest.raises(archive.ArchiveError):
        archive.unpack(_zip_with(data, {"printlog.db": _fresh_db_bytes(), "run.sh": b"x"}), data / "a")
    with pytest.raises(archive.ArchiveError):
        archive.unpack(_zip_with(data, {"printlog.db": _fresh_db_bytes()}, symlink="images/link.jpg"), data / "b")
    with pytest.raises(archive.ArchiveError):
        archive.unpack(_zip_with(data, {"images/a.jpg": b"x"}), data / "c")  # keine Datenbank


def test_reject_bad_db() -> None:
    data = Path(tempfile.mkdtemp())
    with pytest.raises(archive.ArchiveError):
        archive.unpack(_zip_with(data, {"printlog.db": b"kein sqlite"}), data / "a")
    with pytest.raises(archive.ArchiveError):
        archive.unpack(_zip_with(data, {"printlog.db": _fresh_db_bytes(trigger=True)}), data / "b")
    other = Path(tempfile.mkdtemp()) / "o.db"
    sqlite3.connect(other).execute("CREATE TABLE foo (x)").connection.commit()
    with pytest.raises(archive.ArchiveError):
        archive.unpack(_zip_with(data, {"printlog.db": other.read_bytes()}), data / "c")


def test_top_folder_and_macos_junk_ok() -> None:
    data = Path(tempfile.mkdtemp())
    z = _zip_with(data, {"printlog-2026/printlog.db": _fresh_db_bytes(), "printlog-2026/images/a.jpg": b"x",
                         "__MACOSX/printlog-2026/._printlog.db": b"x"})
    info = archive.unpack(z, data / "out")
    assert info["images"] == 1


def test_reset_history_keeps_settings() -> None:
    data, images, store, _ = _setup()
    name = archive.reset(store, images, data, everything=False)
    assert archive.safety_path(data, name) is not None
    assert store.counts()["jobs"] == 0
    assert len(store.customers()) == 1
    assert store.settings()["margin1_pct"] == "20"
    assert sorted(p.name for p in images.iterdir()) == ["bon_logo_1.png"]


def test_reset_everything() -> None:
    data, images, store, _ = _setup()
    store.set_setting("ha_seen_ts", "123")
    archive.reset(store, images, data, everything=True)
    assert store.counts()["jobs"] == 0 and not store.customers()
    assert store.settings() == {"language": "de", "ha_seen_ts": "123"}
    assert not list(images.iterdir())


def test_reset_knows_every_table() -> None:
    """Neue Tabellen müssen in HISTORY_TABLES oder SETTINGS_TABLES stehen, sonst bleiben sie beim Zurücksetzen stehen."""
    _, _, store, _ = _setup()
    tables = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == set(Store.HISTORY_TABLES) | set(Store.SETTINGS_TABLES)


def test_safety_names() -> None:
    data = Path(tempfile.mkdtemp())
    assert archive.safety_path(data, "../printlog.db") is None
    assert archive.safety_path(data, "printlog-vor-import-2026-01-01_120000.zip") is None  # gibt es nicht


def test_error_texts_translated() -> None:
    tree = ast.parse(Path(archive.__file__).read_text(encoding="utf-8"))
    keys = [n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ArchiveError"
            and n.args and isinstance(n.args[0], ast.Constant)]
    assert keys and not [k for k in keys if k not in EN]
