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


def test_notes_v3_columns(isolated_db):
    """Миграция 3: quote и edited_at у заметок (R1/R2)."""
    db.init_db()
    conn = db.connect()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(notes)")]
        cur = conn.execute(
            "INSERT INTO books(title, author, created_at) VALUES('t','a','x')"
        )
        cur = conn.execute(
            "INSERT INTO chapters(book_id, ord, title, html, created_at) "
            "VALUES(?, 1, 'h', '<p>x</p>', 'x')",
            (cur.lastrowid,),
        )
        cur = conn.execute(
            "INSERT INTO notes(chapter_id, text, quote, created_at) VALUES(?, 'т', 'цит', 'x')",
            (cur.lastrowid,),
        )
        row = conn.execute(
            "SELECT quote, edited_at FROM notes WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    assert "quote" in cols and "edited_at" in cols
    assert row["quote"] == "цит" and row["edited_at"] == ""


def test_v4_profiles_and_ratings_migration(isolated_db):
    """Миграция 4: профили, state по профилям, перенос оценок в ratings."""
    # база «из прошлого»: без профилей, оценка в колонке chapters.rating
    conn = sqlite3.connect(isolated_db / "app.db")
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO books(title, author, created_at) VALUES('t','a','x')"
    )
    cur = conn.execute(
        "INSERT INTO chapters(book_id, ord, title, html, rating, created_at) "
        "VALUES(1, 1, 'h', '<p>x</p>', 3, 'x')"
    )
    conn.execute(
        "INSERT INTO state(chapter_id, read_pct, bookmark) VALUES(?, 60, 1)",
        (cur.lastrowid,),
    )
    conn.execute(
        "INSERT INTO notes(chapter_id, text, created_at) VALUES(?, 'n', 'x')",
        (cur.lastrowid,),
    )
    conn.commit()
    conn.close()
    assert db.schema_version() == 0

    db.init_db()
    assert db.schema_version() == db.SCHEMA_VERSION
    conn = db.connect()
    try:
        prof = conn.execute("SELECT name FROM profiles WHERE id = 1").fetchone()
        state_cols = [r["name"] for r in conn.execute("PRAGMA table_info(state)")]
        note_cols = [r["name"] for r in conn.execute("PRAGMA table_info(notes)")]
        rating = conn.execute(
            "SELECT value FROM ratings WHERE target='chapter' AND target_id=1 AND profile_id=1"
        ).fetchone()
        st = conn.execute("SELECT profile_id, read_pct FROM state WHERE chapter_id=1").fetchone()
        np = conn.execute("SELECT profile_id FROM notes WHERE chapter_id=1").fetchone()
    finally:
        conn.close()
    assert prof["name"] == "Я"
    assert "profile_id" in state_cols and "profile_id" in note_cols
    assert rating["value"] == 3
    assert st["profile_id"] == 1 and st["read_pct"] == 60
    assert np["profile_id"] == 1
