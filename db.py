import os
import re
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    title TEXT NOT NULL,
    html TEXT NOT NULL,
    words INTEGER NOT NULL DEFAULT 0,
    rating INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, ord);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    rating INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_chapter ON notes(chapter_id);
CREATE TABLE IF NOT EXISTS state (
    chapter_id INTEGER PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
    bookmark INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    read_pct INTEGER NOT NULL DEFAULT 0,
    last_read_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Версия схемы (baseline = 1). Новые изменения — только через MIGRATIONS:
# ключ = номер версии, значение = SQL-скрипт апгрейда. Порядок применяется
# по возрастанию, каждая миграция выполняется транзакционно и поднимает
# PRAGMA user_version. SCHEMA — только для создания новой базы с нуля.
SCHEMA_VERSION = 3

MIGRATIONS: dict[int, str] = {
    2: """
    CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5(
        title, content, tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER IF NOT EXISTS chapters_fts_del AFTER DELETE ON chapters BEGIN
        DELETE FROM chapters_fts WHERE rowid = old.id;
    END;
    """,
    # 3: цитата к заметке (R1) + дата правки (R2)
    3: """
    ALTER TABLE notes ADD COLUMN quote TEXT NOT NULL DEFAULT '';
    ALTER TABLE notes ADD COLUMN edited_at TEXT NOT NULL DEFAULT '';
    """,
}


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"База данных версии {current} новее кода ({SCHEMA_VERSION}): "
                "обновите приложение, откат схемы не поддерживается"
            )
        for target in sorted(MIGRATIONS):
            if current < target:
                conn.executescript(MIGRATIONS[target])
                conn.execute(f"PRAGMA user_version = {target}")
                current = target
        if current < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _reindex_fts(conn)
        conn.commit()
    finally:
        conn.close()


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _reindex_fts(conn: sqlite3.Connection) -> None:
    """Первая миграция или пустой индекс — наполнить FTS из глав."""
    try:
        fts_count = conn.execute("SELECT COUNT(*) c FROM chapters_fts").fetchone()["c"]
    except sqlite3.OperationalError:
        return
    if fts_count:
        return
    rows = conn.execute("SELECT id, title, html FROM chapters").fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO chapters_fts(rowid, title, content) VALUES (?,?,?)",
            (r["id"], r["title"], strip_html(r["html"])),
        )


def schema_version() -> int:
    conn = connect()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_settings() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def save_setting(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
