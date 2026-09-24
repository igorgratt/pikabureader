import sqlite3

import pytest

import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Все операции db — во временной базе, реальная data/ не трогаем."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    return tmp_path


def test_fresh_db_has_current_version(isolated_db):
    db.init_db()
    assert db.schema_version() == db.SCHEMA_VERSION


def test_init_idempotent(isolated_db):
    db.init_db()
    db.init_db()
    assert db.schema_version() == db.SCHEMA_VERSION


def test_legacy_zero_version_upgraded(isolated_db):
    """База, созданная старым кодом (user_version=0), поднимается до текущей."""
    conn = sqlite3.connect(isolated_db / "app.db")
    conn.executescript(db.SCHEMA)
    conn.commit()
    conn.close()
    assert db.schema_version() == 0
    db.init_db()
    assert db.schema_version() == db.SCHEMA_VERSION


def test_future_version_rejected(isolated_db):
    db.init_db()
    conn = sqlite3.connect(isolated_db / "app.db")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError):
        db.init_db()


def test_migration_applied_in_order(isolated_db, monkeypatch):
    """Миграции применяются по возрастанию и поднимают версию."""
    monkeypatch.setattr(db, "MIGRATIONS", {
        2: "ALTER TABLE books ADD COLUMN lang TEXT NOT NULL DEFAULT ''",
        3: "ALTER TABLE books ADD COLUMN rating INTEGER NOT NULL DEFAULT 0",
    })
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    db.init_db()
    assert db.schema_version() == 3
    conn = db.connect()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(books)")]
    finally:
        conn.close()
    assert "lang" in cols and "rating" in cols


def test_settings_roundtrip(isolated_db):
    db.init_db()
    db.save_setting("theme", "dark")
    db.save_setting("theme", "light")
    db.save_setting("size", "18")
    s = db.get_settings()
    assert s["theme"] == "light" and s["size"] == "18"
