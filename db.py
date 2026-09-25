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
SCHEMA_VERSION = 10

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
    # 4: профили читателей (D2) — свой прогресс, заметки и оценки у каждого
    4: """
    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );
    INSERT OR IGNORE INTO profiles(id, name, created_at)
        VALUES (1, 'Я', datetime('now'));

    ALTER TABLE notes ADD COLUMN profile_id INTEGER NOT NULL DEFAULT 1;

    CREATE TABLE IF NOT EXISTS ratings (
        target TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        profile_id INTEGER NOT NULL,
        value INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (target, target_id, profile_id)
    );
    INSERT OR IGNORE INTO ratings(target, target_id, profile_id, value)
        SELECT 'chapter', id, 1, rating FROM chapters WHERE rating != 0;
    INSERT OR IGNORE INTO ratings(target, target_id, profile_id, value)
        SELECT 'note', id, 1, rating FROM notes WHERE rating != 0;

    CREATE TABLE state_profiled (
        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        profile_id INTEGER NOT NULL,
        bookmark INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0,
        read_pct INTEGER NOT NULL DEFAULT 0,
        last_read_at TEXT,
        PRIMARY KEY (chapter_id, profile_id)
    );
    INSERT INTO state_profiled(chapter_id, profile_id, bookmark, done, read_pct, last_read_at)
        SELECT chapter_id, 1, bookmark, done, read_pct, last_read_at FROM state;
    DROP TABLE state;
    ALTER TABLE state_profiled RENAME TO state;
    """,
    # 5: цитата закладки (N6) — абзац, на который ведёт закладка
    5: """
    ALTER TABLE state ADD COLUMN bookmark_quote TEXT NOT NULL DEFAULT '';
    """,
    # 6: файл-источник главы — внутрикнижные ссылки (сноски) → /goto
    6: """
    ALTER TABLE chapters ADD COLUMN source TEXT NOT NULL DEFAULT '';
    """,
    # 7: data-only — опасные схемы URL (javascript:/data:) в старых импортах
    # вырезаются python-бэкфиллом в init_db (см. start < 7)
    7: """
    SELECT 1;
    """,
    # 8: R7 — журнал чтения по дням (слов на день, серия дней)
    8: """
    CREATE TABLE IF NOT EXISTS read_log (
        profile_id INTEGER NOT NULL DEFAULT 1,
        day TEXT NOT NULL,
        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        words INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (profile_id, day, chapter_id)
    );
    """,
    # 9: D7 — публикация книги по ссылке (read-only, токен)
    9: """
    CREATE TABLE IF NOT EXISTS share_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT
    );
    """,
    # 10: M6 — синхронизация между устройствами (device tokens + sync API)
    10: """
    CREATE TABLE IF NOT EXISTS sync_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL UNIQUE,
        device_name TEXT NOT NULL DEFAULT '',
        profile_id INTEGER NOT NULL DEFAULT 1 REFERENCES profiles(id),
        created_at TEXT NOT NULL,
        last_seen_at TEXT
    );

    CREATE TABLE IF NOT EXISTS sync_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        profile_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(entity, entity_id, profile_id)
    );
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
        start = current
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
            # нельзя молча поднять версию: колонки бы не появились
            raise RuntimeError(
                f"Нет миграции до версии схемы {SCHEMA_VERSION} "
                f"(база осталась на {current}) — добавьте запись в MIGRATIONS"
            )
        if start < 6:
            # разовая правка старых импортов: относительные ссылки на файлы
            # книги (../Text/notes.xhtml#...) вели в 404 — снимаем href,
            # текст ссылки остаётся (у новых книг ссылки идут через /goto)
            rows = conn.execute("SELECT id, html FROM chapters").fetchall()
            for r in rows:
                fixed = neutralize_dead_links(r["html"])
                if fixed != r["html"]:
                    conn.execute(
                        "UPDATE chapters SET html = ? WHERE id = ?", (fixed, r["id"])
                    )
        if start < 7:
            # Q8: в старых импортах могли сохраниться href/src с опасными
            # схемами (javascript:, data:) — вырезаем, как при новом импорте
            from parsers import _safe_url  # лениво: db не тянет ebooklib без нужды

            attr = re.compile(r'(\s(?:href|src)\s*=\s*)(["\'])([^"\']*)\2', re.I)

            def _fix(m: re.Match) -> str:
                return "" if not _safe_url(m.group(3)) else m.group(0)

            rows = conn.execute("SELECT id, html FROM chapters").fetchall()
            for r in rows:
                fixed = attr.sub(_fix, r["html"] or "")
                if fixed != r["html"]:
                    conn.execute(
                        "UPDATE chapters SET html = ? WHERE id = ?", (fixed, r["id"])
                    )
        _reindex_fts(conn)
        conn.commit()
    finally:
        conn.close()


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


_A_TAG = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_A_HREF = re.compile(r'href="([^"]*)"', re.I)


def neutralize_dead_links(html: str) -> str:
    """Снять href у относительных ссылок на файлы книги (старые импорты
    давали 404: /Text/notes.xhtml). Якоря, абсолютные и app-ссылки — целы."""
    def repl(m: re.Match) -> str:
        hm = _A_HREF.search(m.group(1))
        if not hm:
            return m.group(0)
        h = hm.group(1).strip()
        if h.startswith(("#", "/", "http:", "https:", "mailto:", "asset:", "data:")):
            return m.group(0)
        if re.search(r"\.(?:x?html?|xml)(?:#|$)", h, re.I) or h.startswith(("./", "../")):
            return m.group(2)  # ссылка мертва: оставляем текст
        return m.group(0)

    return _A_TAG.sub(repl, html)


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


def reindex_fts() -> int:
    """Полная переиндексация FTS (D10: кнопка в панели владельца)."""
    conn = connect()
    try:
        conn.execute("DELETE FROM chapters_fts")
        _reindex_fts(conn)
        conn.commit()
        return conn.execute("SELECT COUNT(*) c FROM chapters_fts").fetchone()["c"]
    finally:
        conn.close()


def default_profile_id() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO profiles(id, name, created_at) VALUES (1, 'Я', ?)",
                (now(),),
            )
            conn.commit()
            return 1
        return row["id"]
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    """Сегодняшний день (UTC) — для дневной статистики чтения (R7)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


# ---------------------------------------------------------------- M6: sync

def sync_token_validate(token: str) -> dict | None:
    """Валидирует токен устройства. Возвращает {profile_id, device_name} или None."""
    conn = connect()
    try:
        row = conn.execute(
            "UPDATE sync_tokens SET last_seen_at = ? WHERE token = ? RETURNING profile_id, device_name",
            (now(), token),
        ).fetchone()
        if row:
            conn.commit()
            return {"profile_id": row["profile_id"], "device_name": row["device_name"] or "Устройство"}
        return None
    finally:
        conn.close()


def sync_token_create(profile_id: int, device_name: str) -> str:
    """Создаёт новый токен устройства. Возвращает токен."""
    import secrets
    token = secrets.token_urlsafe(24)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sync_tokens(token, profile_id, device_name, created_at) VALUES (?, ?, ?, ?)",
            (token, profile_id, device_name, now()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def sync_state_get(profile_id: int) -> dict:
    """Возвращает полное состояние для профиля: книги, главы, прогресс, заметки, рейтинги."""
    conn = connect()
    try:
        books = [
            dict(r) for r in conn.execute(
                "SELECT id, title, author, cover, description, tags FROM books ORDER BY id"
            ).fetchall()
        ]
        chapters = [
            dict(r) for r in conn.execute(
                "SELECT id, book_id, ord, title, words FROM chapters ORDER BY id"
            ).fetchall()
        ]
        state = [
            dict(r) for r in conn.execute(
                "SELECT * FROM state WHERE profile_id = ?", (profile_id,)
            ).fetchall()
        ]
        notes = [
            dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE profile_id = ?", (profile_id,)
            ).fetchall()
        ]
        ratings = [
            dict(r) for r in conn.execute(
                "SELECT * FROM ratings WHERE profile_id = ?", (profile_id,)
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "profile_id": profile_id,
        "books": books,
        "chapters": chapters,
        "state": state,
        "notes": notes,
        "ratings": ratings,
        "server_time": now(),
    }


def sync_state_merge(profile_id: int, client_state: dict) -> dict:
    """Принимает состояние от клиента, мержит с серверным (last-write-wins),
    возвращает итоговое состояние."""
    conn = connect()
    try:
        now_ts = now()

        # books — read-only при sync, не мержим

        # state — last-write-wins по chapter_id+profile_id
        for s in client_state.get("state", []):
            cid = s.get("chapter_id")
            if not cid:
                continue
            existing = conn.execute(
                "SELECT read_pct, done, bookmark, last_read_at FROM state "
                "WHERE chapter_id = ? AND profile_id = ?",
                (cid, profile_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO state(chapter_id, profile_id, read_pct, done, bookmark,
                       bookmark_quote, last_read_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cid, profile_id, s.get("read_pct", 0), s.get("done", 0),
                     s.get("bookmark", 0), s.get("bookmark_quote", ""),
                     s.get("last_read_at") or now_ts),
                )
            else:
                # last-write-wins: клиент побеждает если его timestamp новее
                client_ts = s.get("last_read_at", "")
                server_ts = existing["last_read_at"] or ""
                if client_ts and (not server_ts or client_ts > server_ts):
                    conn.execute(
                        """UPDATE state SET read_pct = ?, done = ?, bookmark = ?,
                           bookmark_quote = ?, last_read_at = ?
                           WHERE chapter_id = ? AND profile_id = ?""",
                        (s.get("read_pct", 0), s.get("done", 0), s.get("bookmark", 0),
                         s.get("bookmark_quote", ""), client_ts, cid, profile_id),
                    )

        # notes — last-write-wins по id (edited_at)
        for n in client_state.get("notes", []):
            nid = n.get("id")
            if not nid:
                continue
            existing = conn.execute(
                "SELECT edited_at FROM notes WHERE id = ? AND profile_id = ?",
                (nid, profile_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO notes(id, chapter_id, profile_id, text, rating, quote,
                       parent_id, created_at, edited_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (nid, n.get("chapter_id"), profile_id, n.get("text", ""),
                     n.get("rating", 0), n.get("quote", ""),
                     n.get("parent_id"), n.get("created_at") or now_ts,
                     n.get("edited_at") or now_ts),
                )
            else:
                client_ts = n.get("edited_at", "")
                server_ts = existing["edited_at"] or ""
                if client_ts and (not server_ts or client_ts > server_ts):
                    conn.execute(
                        """UPDATE notes SET text = ?, rating = ?, quote = ?,
                           parent_id = ?, edited_at = ?
                           WHERE id = ? AND profile_id = ?""",
                        (n.get("text", ""), n.get("rating", 0), n.get("quote", ""),
                         n.get("parent_id"), client_ts, nid, profile_id),
                    )

        # ratings — last-write-wins по target+target_id+profile_id
        for r in client_state.get("ratings", []):
            t = r.get("target")
            tid = r.get("target_id")
            if not t or not tid:
                continue
            val = r.get("value", 0)
            conn.execute(
                """INSERT INTO ratings(target, target_id, profile_id, value)
                   VALUES (?,?,?,?)
                   ON CONFLICT(target, target_id, profile_id) DO UPDATE SET value = ?""",
                (t, tid, profile_id, val, val),
            )

        conn.commit()
        return sync_state_get(profile_id)
    finally:
        conn.close()
