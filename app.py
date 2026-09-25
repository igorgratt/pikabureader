import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from functools import wraps
from html import escape as _escape

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)

import db
from parsers import parse_book

__version__ = "0.4.1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
IMG_DIR = os.path.join(db.DATA_DIR, "images")
COVER_DIR = os.path.join(db.DATA_DIR, "covers")
IMPORT_LOG = os.path.join(db.DATA_DIR, "import.log")

app = Flask(__name__)


def _load_secret_key() -> str:
    """Секрет сессий: env SECRET_KEY, иначе случайный, сохранённый в data/."""
    env = os.environ.get("SECRET_KEY", "")
    if env:
        return env
    path = os.path.join(db.DATA_DIR, "secret_key")
    try:
        with open(path, encoding="ascii") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = uuid.uuid4().hex + uuid.uuid4().hex
    os.makedirs(db.DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(key)
    return key


app.secret_key = _load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["VERSION"] = __version__


def get_db() -> sqlite3.Connection:
    return db.connect()


# ---------------------------------------------------------------- D3: пароль

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 120_000
    ).hex()


def _set_password(password: str) -> None:
    """Задать пароль инстанса; пустая строка — снять пароль."""
    if not password:
        db.save_setting("password_hash", "")
        return
    salt = uuid.uuid4().hex[:16]
    db.save_setting("password_hash", f"{salt}:{_hash_password(password, salt)}")


def _check_password(stored: str, password: str) -> bool:
    salt, _, expected = stored.partition(":")
    if not salt or not expected:
        return False
    return hmac.compare_digest(_hash_password(password, salt), expected)


@app.before_request
def _gate():
    """Доступ к инстансу: если задан пароль — требуется вход (D3)."""
    ep = request.endpoint
    if ep in ("static", "login", "logout", "healthz"):
        return None
    stored = db.get_settings().get("password_hash", "")
    if not stored or session.get("owner"):
        return None
    if request.path.startswith("/api/"):
        return Response(
            json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False),
            status=401, mimetype="application/json",
        )
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        stored = db.get_settings().get("password_hash", "")
        if stored and _check_password(stored, password):
            session["owner"] = 1
            nxt = request.args.get("next") or ""
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("feed"))
        flash("Неверный пароль")
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """Лёгкий healthcheck для Docker/CI: без БД, без авторизации."""
    return Response(
        json.dumps({"ok": True, "version": app.config["VERSION"]}),
        mimetype="application/json",
    )


# ---------------------------------------------------------------- D2: профили

def _profile_id() -> int:
    pid = session.get("profile_id")
    if pid:
        return int(pid)
    pid = db.default_profile_id()
    session["profile_id"] = pid
    return pid


def _profile_name(pid: int) -> str:
    conn = get_db()
    try:
        row = conn.execute("SELECT name FROM profiles WHERE id = ?", (pid,)).fetchone()
        return row["name"] if row else "Я"
    finally:
        conn.close()


@app.template_filter("pct")
def pct(value) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


@app.template_filter("num")
def num(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


@app.context_processor
def inject_globals():
    conn = get_db()
    pid = _profile_id()
    try:
        counts = {
            "feed": conn.execute("SELECT COUNT(*) c FROM chapters").fetchone()["c"],
            "books": conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"],
            "bookmarks": conn.execute(
                "SELECT COUNT(*) c FROM state WHERE bookmark = 1 AND profile_id = ?",
                (pid,),
            ).fetchone()["c"],
            "reading": conn.execute(
                "SELECT COUNT(*) c FROM state WHERE done = 0 AND read_pct > 0 AND profile_id = ?",
                (pid,),
            ).fetchone()["c"],
        }
        books = conn.execute("SELECT * FROM books ORDER BY id DESC").fetchall()
        popular = conn.execute(
            """SELECT c.id, c.title, b.title bt, c.rating
               FROM chapters c JOIN books b ON b.id = c.book_id
               ORDER BY c.rating DESC, c.id DESC LIMIT 5"""
        ).fetchall()
        reading = conn.execute(
            """SELECT c.id, c.title, b.title bt, s.read_pct
               FROM state s JOIN chapters c ON c.id = s.chapter_id
               JOIN books b ON b.id = c.book_id
               WHERE s.done = 0 AND s.read_pct > 0 AND s.profile_id = ?
               ORDER BY s.last_read_at DESC LIMIT 5""",
            (pid,),
        ).fetchall()
        profiles = conn.execute(
            "SELECT id, name FROM profiles ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    tab = request.args.get("t", "")
    current_tab = tab if tab in ("hot", "new", "library", "reading") else ("new" if request.path == "/" else "")
    if request.path.startswith("/book/"):
        current_tab = "library"
    return {
        "nav_counts": counts, "current_tab": current_tab,
        "settings": db.get_settings(), "books": books,
        "popular": popular, "reading": reading,
        "app_version": __version__,
        "profiles": profiles, "current_profile": pid,
        "profile_name": _profile_name(pid),
    }


# ---------------------------------------------------------------- media

@app.route("/media/images/<int:book_id>/<path:name>")
def media_image(book_id: int, name: str):
    return send_from_directory(os.path.join(IMG_DIR, f"book_{book_id}"), name)


@app.route("/covers/<path:name>")
def cover(name: str):
    return send_from_directory(COVER_DIR, name)


# ---------------------------------------------------------------- feed

@app.template_filter("preview")
def preview(value, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 200 else cut).rstrip() + "…"


def _hl_snippet(raw: str) -> str:
    """Экранировать текст сниппета, сохранив маркеры подсветки из FTS."""
    return _escape(raw).replace("\x01", "<mark>").replace("\x02", "</mark>")


def _fts_query(q: str) -> str:
    return " AND ".join(f'"{t}"' for t in re.findall(r"\w+", q, flags=re.UNICODE))


def _search_chapters(conn, q: str, limit: int = 8):
    """N1: полнотекстовый поиск по главам (FTS5) с контекстом."""
    terms = _fts_query(q)
    if not terms:
        return []
    try:
        hits = conn.execute(
            """SELECT c.id, c.title, b.title bt,
                      snippet(chapters_fts, 1, char(1), char(2), '…', 16) snip
               FROM chapters_fts
               JOIN chapters c ON c.id = chapters_fts.rowid
               JOIN books b ON b.id = c.book_id
               WHERE chapters_fts MATCH ? LIMIT ?""",
            (terms, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"id": h["id"], "title": h["title"], "bt": h["bt"],
             "snip": _hl_snippet(h["snip"])} for h in hits]


def _render_feed(conn, order: str, page: int, q: str = "", per_page: int = 10):
    offset = (page - 1) * per_page
    pid = _profile_id()
    where = ""
    params: list = []
    if q:
        where = "WHERE (c.title LIKE ? OR b.title LIKE ? OR b.author LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if order == "hot":
        order_by = "c.rating DESC, c.id DESC"
    elif order == "reading":
        where = (where + " AND" if where else "WHERE") + " s.done = 0 AND s.read_pct > 0"
        order_by = "s.last_read_at DESC"
    else:
        order_by = "c.id DESC"
    sql = f"""
        SELECT c.*, b.title book_title, b.author, b.cover, b.id bid,
               s.read_pct, s.bookmark, s.done,
               (SELECT COUNT(*) FROM notes n WHERE n.chapter_id = c.id
                    AND n.profile_id = ?) note_count
        FROM chapters c JOIN books b ON b.id = c.book_id
        LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
        {where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, (pid, pid, *params, per_page, offset)).fetchall()
    count_sql = f"SELECT COUNT(*) c FROM chapters c JOIN books b ON b.id = c.book_id LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ? {where}"
    total = conn.execute(count_sql, (pid, *params)).fetchone()["c"]
    return rows, total


@app.route("/")
def feed():
    tab = request.args.get("t", "new")
    page = max(1, int(request.args.get("p", 1)))
    q = request.args.get("q", "").strip()
    conn = get_db()
    try:
        chapter_hits = _search_chapters(conn, q) if q else []
        if tab == "library":
            books = conn.execute(
                "SELECT * FROM books WHERE title LIKE ? OR author LIKE ? ORDER BY id DESC",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
            return render_template("library.html", books=books, q=q)
        order = tab if tab in ("hot", "reading") else "new"
        rows, total = _render_feed(conn, order, page, q)
    finally:
        conn.close()
    pages = max(1, (total + 9) // 10)
    return render_template(
        "feed.html", rows=rows, tab=tab, page=page, pages=pages, q=q,
        total=total, chapter_hits=chapter_hits,
    )


@app.route("/bookmarks")
def bookmarks():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT c.*, b.title book_title, b.author, b.cover, b.id bid,
                      s.read_pct, s.bookmark, s.done
               FROM state s JOIN chapters c ON c.id = s.chapter_id
               JOIN books b ON b.id = c.book_id
               WHERE s.bookmark = 1 AND s.profile_id = ?
               ORDER BY s.last_read_at DESC""",
            (_profile_id(),),
        ).fetchall()
    finally:
        conn.close()
    return render_template("list.html", rows=rows, heading="Закладки")


# ---------------------------------------------------------------- book

@app.route("/book/<int:book_id>")
def book_page(book_id: int):
    conn = get_db()
    pid = _profile_id()
    try:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            abort(404)
        rows = conn.execute(
            """SELECT c.*, s.read_pct, s.bookmark, s.done
               FROM chapters c
               LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
               WHERE c.book_id = ? ORDER BY c.ord""",
            (pid, book_id),
        ).fetchall()
        progress = conn.execute(
            """SELECT COALESCE(SUM(s.done), 0) done, COUNT(*) total
               FROM chapters c LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
               WHERE c.book_id = ?""",
            (pid, book_id),
        ).fetchone()
        # M9: куда вести «Продолжить чтение»
        resume = conn.execute(
            """SELECT c.id FROM chapters c
               JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
               WHERE c.book_id = ? AND s.done = 0 AND s.read_pct > 0
               ORDER BY s.last_read_at DESC LIMIT 1""",
            (pid, book_id),
        ).fetchone()
        resume_id = resume["id"] if resume else None
        resume_label = "Продолжить чтение" if resume else ""
        if resume_id is None:
            nxt_unread = conn.execute(
                """SELECT c.id FROM chapters c
                   LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
                   WHERE c.book_id = ? AND COALESCE(s.done, 0) = 0
                   ORDER BY c.ord LIMIT 1""",
                (pid, book_id),
            ).fetchone()
            if nxt_unread:
                resume_id = nxt_unread["id"]
                resume_label = "Читать"
            else:
                first = conn.execute(
                    "SELECT id FROM chapters WHERE book_id = ? ORDER BY ord LIMIT 1",
                    (book_id,),
                ).fetchone()
                if first:
                    resume_id = first["id"]
                    resume_label = "Перечитать"
    finally:
        conn.close()
    pct_book = int(progress["done"] * 100 / progress["total"]) if progress["total"] else 0
    return render_template(
        "book.html", book=book, rows=rows, pct_book=pct_book,
        resume_id=resume_id, resume_label=resume_label,
    )


# ---------------------------------------------------------------- story (chapter)

@app.route("/story/<int:chapter_id>")
def story(chapter_id: int):
    conn = get_db()
    pid = _profile_id()
    try:
        ch = conn.execute(
            """SELECT c.*, b.title book_title, b.author, b.cover, b.id bid,
                      s.read_pct, s.bookmark, s.done
               FROM chapters c JOIN books b ON b.id = c.book_id
               LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
               WHERE c.id = ?""",
            (pid, chapter_id),
        ).fetchone()
        if ch is None:
            abort(404)
        prev = conn.execute(
            "SELECT id, title FROM chapters WHERE book_id = ? AND ord < ? ORDER BY ord DESC LIMIT 1",
            (ch["bid"], ch["ord"]),
        ).fetchone()
        nxt = conn.execute(
            "SELECT id, title FROM chapters WHERE book_id = ? AND ord > ? ORDER BY ord LIMIT 1",
            (ch["bid"], ch["ord"]),
        ).fetchone()
        notes = conn.execute(
            "SELECT * FROM notes WHERE chapter_id = ? AND profile_id = ? ORDER BY id",
            (chapter_id, pid),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) c FROM chapters WHERE book_id = ?", (ch["bid"],)
        ).fetchone()["c"]
        # N3: оглавление книги с прогрессом по главам
        toc = conn.execute(
            """SELECT c.id, c.ord, c.title, s.read_pct, s.done
               FROM chapters c
               LEFT JOIN state s ON s.chapter_id = c.id AND s.profile_id = ?
               WHERE c.book_id = ? ORDER BY c.ord""",
            (pid, ch["bid"]),
        ).fetchall()
    finally:
        conn.close()

    # build note tree
    tree: dict = {None: []}
    nodes: dict = {}
    for n in notes:
        node = {"id": n["id"], "text": n["text"], "rating": n["rating"],
                "quote": n["quote"], "edited_at": n["edited_at"],
                "created_at": n["created_at"], "children": []}
        nodes[n["id"]] = node
    for n in notes:
        pid = n["parent_id"]
        if pid in nodes:
            nodes[pid]["children"].append(nodes[n["id"]])
        else:
            tree[None].append(nodes[n["id"]])
    return render_template(
        "story.html", ch=ch, prev=prev, nxt=nxt, note_tree=tree[None],
        book_pos=ch["ord"], book_total=total, toc=toc,
    )


# ---------------------------------------------------------------- add

@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "GET":
        return render_template("add.html")
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Файл не выбран")
        return redirect(url_for("add"))
    filename = re.sub(r"[^\w.\-]+", "_", file.filename)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{filename}")
    file.save(path)
    try:
        parsed = parse_book(path)
    except Exception as exc:  # noqa: BLE001
        flash(f"Не удалось разобрать книгу: {exc}")
        return redirect(url_for("add"))
    if not parsed.chapters:
        flash("В книге не найдено ни одной главы")
        return redirect(url_for("add"))

    conn = get_db()
    try:
        # F4: дедупликация — та же книга уже в библиотеке
        dup = conn.execute(
            "SELECT id FROM books WHERE lower(title) = lower(?) AND lower(author) = lower(?)",
            (parsed.title, parsed.author),
        ).fetchone()
        if dup:
            os.remove(path)
            flash(f"Книга «{parsed.title}» уже есть в библиотеке — добавление пропущено")
            return redirect(url_for("book_page", book_id=dup["id"]))

        cur = conn.execute(
            """INSERT INTO books(title, author, cover, tags, description, created_at)
               VALUES(?,?,?,?,?,?)""",
            (parsed.title, parsed.author, "", parsed.tags, parsed.description, db.now()),
        )
        book_id = cur.lastrowid

        # save assets
        if parsed.assets:
            folder = os.path.join(IMG_DIR, f"book_{book_id}")
            os.makedirs(folder, exist_ok=True)
            mapping = {}
            for archive_name, (short, data) in parsed.assets.items():
                ext = os.path.splitext(short)[1] or ".jpg"
                target = f"{os.path.splitext(short)[0]}{ext}"
                with open(os.path.join(folder, target), "wb") as f:
                    f.write(data)
                mapping[short] = target
        else:
            mapping = {}

        # cover
        cover_name = ""
        if parsed.cover:
            cover_name = f"book_{book_id}.jpg"
            os.makedirs(COVER_DIR, exist_ok=True)
            with open(os.path.join(COVER_DIR, cover_name), "wb") as f:
                f.write(parsed.cover)

        for ord_, (title, html) in enumerate(parsed.chapters, 1):
            html = html.replace("asset:", f"/media/images/{book_id}/")
            for short, target in mapping.items():
                html = html.replace(f"/media/images/{book_id}/{short}", f"/media/images/{book_id}/{target}")
            text = re.sub(r"<[^>]+>", "", html)
            words = len(text.split())
            cur = conn.execute(
                """INSERT INTO chapters(book_id, ord, title, html, words, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (book_id, ord_, title, html, words, db.now()),
            )
            try:
                conn.execute(
                    "INSERT INTO chapters_fts(rowid, title, content) VALUES (?,?,?)",
                    (cur.lastrowid, title, db.strip_html(html)),
                )
            except sqlite3.OperationalError:
                pass  # FTS появится после миграции init_db
        if cover_name:
            conn.execute("UPDATE books SET cover = ? WHERE id = ?", (cover_name, book_id))
        conn.commit()
        flash(f"Книга «{parsed.title}» добавлена: {len(parsed.chapters)} глав")
        return redirect(url_for("book_page", book_id=book_id))
    finally:
        conn.close()


# ---------------------------------------------------------------- api

def json_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            data = fn(*args, **kwargs)
            return Response(json.dumps(data, ensure_ascii=False), mimetype="application/json")
        except Exception as exc:  # noqa: BLE001
            return Response(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), mimetype="application/json"), 400

    return wrapper


@app.post("/api/rate")
@json_api
def api_rate():
    payload = request.get_json(force=True)
    target = payload.get("target")  # chapter | note
    delta = int(payload.get("delta", 0))
    tid = int(payload.get("id", 0))
    if target not in ("chapter", "note") or delta not in (-1, 1) or tid <= 0:
        raise ValueError("bad payload")
    pid = _profile_id()
    table = "chapters" if target == "chapter" else "notes"
    conn = get_db()
    try:
        # свой голос профиля: один плюс или один минус (D2)
        row = conn.execute(
            "SELECT value FROM ratings WHERE target = ? AND target_id = ? AND profile_id = ?",
            (target, tid, pid),
        ).fetchone()
        mine = max(-1, min(1, (row["value"] if row else 0) + delta)) if row else delta
        conn.execute(
            """INSERT INTO ratings(target, target_id, profile_id, value)
               VALUES(?,?,?,?)
               ON CONFLICT(target, target_id, profile_id) DO UPDATE SET value = excluded.value""",
            (target, tid, pid, mine),
        )
        # агрегат по всем профилям → колонка rating
        conn.execute(
            f"""UPDATE {table} SET rating =
                    (SELECT COALESCE(SUM(value), 0) FROM ratings
                     WHERE target = ? AND target_id = ?)
                WHERE id = ?""",
            (target, tid, tid),
        )
        conn.commit()
        agg = conn.execute(
            f"SELECT rating FROM {table} WHERE id = ?", (tid,)
        ).fetchone()
    finally:
        conn.close()
    if agg is None:
        raise ValueError("not found")
    return {"ok": True, "rating": agg["rating"], "mine": mine}


@app.post("/api/progress")
@json_api
def api_progress():
    payload = request.get_json(force=True)
    cid = int(payload.get("chapter_id", 0))
    pct_ = max(0, min(100, int(payload.get("pct", 0))))
    done = 1 if payload.get("done") else 0
    pid = _profile_id()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO state(chapter_id, profile_id, read_pct, done, last_read_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chapter_id, profile_id) DO UPDATE SET
                 read_pct = MAX(state.read_pct, excluded.read_pct),
                 done = MAX(state.done, excluded.done),
                 last_read_at = excluded.last_read_at""",
            (cid, pid, pct_, done, db.now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/bookmark")
@json_api
def api_bookmark():
    payload = request.get_json(force=True)
    cid = int(payload.get("chapter_id", 0))
    pid = _profile_id()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO state(chapter_id, profile_id, bookmark, last_read_at)
               VALUES(?,?,1,?) ON CONFLICT(chapter_id, profile_id)
               DO UPDATE SET bookmark = 1 - state.bookmark, last_read_at = excluded.last_read_at""",
            (cid, pid, db.now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT bookmark FROM state WHERE chapter_id = ? AND profile_id = ?",
            (cid, pid),
        ).fetchone()
    finally:
        conn.close()
    return {"ok": True, "bookmark": bool(row and row["bookmark"])}


@app.post("/api/note")
@json_api
def api_note():
    payload = request.get_json(force=True)
    cid = int(payload.get("chapter_id", 0))
    parent = payload.get("parent_id")
    text = (payload.get("text") or "").strip()
    # R1: цитата выделенного текста (обрезаем до разумной длины)
    quote = (payload.get("quote") or "").strip()[:300]
    if not text or cid <= 0:
        raise ValueError("empty note")
    pid = _profile_id()
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO notes(chapter_id, parent_id, text, quote, profile_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (cid, int(parent) if parent else None, text, quote, pid, db.now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "note": dict(row)}


@app.post("/api/note/edit")
@json_api
def api_note_edit():
    """R2: правка своей заметки — текст + дата правки."""
    payload = request.get_json(force=True)
    nid = int(payload.get("id", 0))
    text = (payload.get("text") or "").strip()
    if not text or nid <= 0:
        raise ValueError("empty note")
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE notes SET text = ?, edited_at = ? WHERE id = ? AND profile_id = ?",
            (text, db.now(), nid, _profile_id()),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("note not found")
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "note": dict(row)}


@app.post("/api/note/delete")
@json_api
def api_note_delete():
    payload = request.get_json(force=True)
    nid = int(payload.get("id", 0))
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM notes WHERE id = ? AND profile_id = ?",
            (nid, _profile_id()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/book/delete")
@json_api
def api_book_delete():
    import shutil

    payload = request.get_json(force=True)
    book_id = int(payload.get("book_id", 0))
    conn = get_db()
    try:
        row = conn.execute("SELECT id, cover FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise ValueError("Книга не найдена")
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(os.path.join(IMG_DIR, f"book_{book_id}"), ignore_errors=True)
    if row["cover"]:
        try:
            os.remove(os.path.join(COVER_DIR, row["cover"]))
        except OSError:
            pass
    return {"ok": True}


@app.post("/api/settings")
@json_api
def api_settings():
    payload = request.get_json(force=True)
    for key in ("theme", "font", "size", "width"):
        if key in payload:
            db.save_setting(key, str(payload[key]))
    return {"ok": True, "settings": db.get_settings()}


@app.get("/api/settings")
@json_api
def api_settings_get():
    return {"ok": True, "settings": db.get_settings()}


# ---------------------------------------------------------------- M4: бэкап

def _data_size() -> int:
    total = 0
    for root, _, files in os.walk(db.DATA_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _fmt_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


@app.get("/settings")
def settings_page():
    conn = get_db()
    try:
        stats = {
            "books": conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"],
            "chapters": conn.execute("SELECT COUNT(*) c FROM chapters").fetchone()["c"],
            "notes": conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"],
        }
    finally:
        conn.close()
    return render_template(
        "settings.html", stats=stats, data_size=_fmt_size(_data_size()),
        schema_version=db.schema_version(),
    )


@app.get("/api/backup.zip")
def backup_download():
    """M4: экспорт data/ (база, картинки, обложки) в zip."""
    import io
    import zipfile
    from datetime import datetime, timezone

    if not os.path.exists(db.DB_PATH):
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(db.DATA_DIR):
            for f in files:
                p = os.path.join(root, f)
                zf.write(p, os.path.relpath(p, db.DATA_DIR))
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=f"pikabureader-backup-{stamp}.zip",
    )


@app.post("/api/backup/restore")
def backup_restore():
    """M4: восстановление data/ из zip. Ничего не трогаем, пока архив
    не прочитан и не прошёл проверку."""
    import shutil
    import tempfile
    import zipfile

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Файл бэкапа не выбран")
        return redirect(url_for("settings_page"))
    if not file.filename.lower().endswith(".zip"):
        flash("Ожидается zip-архив, скачанный через «Скачать бэкап»")
        return redirect(url_for("settings_page"))

    tmp = tempfile.mkdtemp(prefix="pikabu-restore-")
    try:
        # 1. распаковка во временный каталог (защита от zip slip)
        try:
            with zipfile.ZipFile(file) as zf:
                dest = os.path.realpath(tmp)
                for name in zf.namelist():
                    target = os.path.realpath(os.path.join(tmp, name))
                    if target != dest and not target.startswith(dest + os.sep):
                        raise ValueError(f"небезопасный путь в архиве: {name}")
                zf.extractall(tmp)
        except Exception as exc:  # noqa: BLE001
            flash(f"Не удалось прочитать архив: {exc}")
            return redirect(url_for("settings_page"))

        # 2. проверка, что это действительно бэкап с живой базой
        dbfile = os.path.join(tmp, "app.db")
        if not os.path.exists(dbfile):
            flash("В архиве нет app.db — это не бэкап PikaBuReader")
            return redirect(url_for("settings_page"))
        try:
            probe = sqlite3.connect(dbfile)
            probe.execute("PRAGMA quick_check").fetchone()
            probe.close()
        except sqlite3.DatabaseError as exc:
            flash(f"База в архиве повреждена: {exc}")
            return redirect(url_for("settings_page"))

        # 3. замена data/ (активных соединений между запросами нет)
        try:
            for sub in ("images", "covers", "uploads"):
                shutil.rmtree(os.path.join(db.DATA_DIR, sub), ignore_errors=True)
            os.replace(dbfile, db.DB_PATH)
            for sub in ("images", "covers", "uploads"):
                src = os.path.join(tmp, sub)
                if os.path.isdir(src):
                    shutil.move(src, os.path.join(db.DATA_DIR, sub))
            db.init_db()  # поднять миграции, если бэкап от старой версии
        except (OSError, sqlite3.Error) as exc:
            flash(f"Ошибка при восстановлении: {exc}")
            return redirect(url_for("settings_page"))

        flash("Бэкап восстановлен")
        return redirect(url_for("feed"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- Q6: лог импорта

def _log_import(action: str, detail: str) -> None:
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(IMPORT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{db.now()} | {action} | {detail}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- D2: UI профилей

@app.route("/profiles", methods=["GET", "POST"])
def profiles_page():
    if request.method == "POST":
        action = request.form.get("action", "")
        name = (request.form.get("name") or "").strip()[:40]
        pid_in = request.form.get("id", "")
        conn = get_db()
        try:
            if action == "create":
                if not name:
                    flash("Имя не может быть пустым")
                else:
                    try:
                        conn.execute(
                            "INSERT INTO profiles(name, created_at) VALUES(?, ?)",
                            (name, db.now()),
                        )
                        conn.commit()
                        flash(f"Профиль «{name}» создан — теперь можно переключиться")
                    except sqlite3.IntegrityError:
                        flash("Профиль с таким именем уже есть")
            elif action == "select" and pid_in:
                row = conn.execute(
                    "SELECT name FROM profiles WHERE id = ?", (int(pid_in),)
                ).fetchone()
                if row:
                    session["profile_id"] = int(pid_in)
                    flash(f"Теперь читает: {row['name']}")
            elif action == "rename" and pid_in and name:
                try:
                    conn.execute(
                        "UPDATE profiles SET name = ? WHERE id = ?",
                        (name, int(pid_in)),
                    )
                    conn.commit()
                    flash("Профиль переименован")
                except sqlite3.IntegrityError:
                    flash("Профиль с таким именем уже есть")
            elif action == "delete" and pid_in:
                pid = int(pid_in)
                count = conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
                if count <= 1:
                    flash("Нельзя удалить последний профиль")
                else:
                    affected_ch = [
                        r["target_id"]
                        for r in conn.execute(
                            "SELECT target_id FROM ratings WHERE profile_id = ? AND target = 'chapter'",
                            (pid,),
                        ).fetchall()
                    ]
                    affected_n = [
                        r["target_id"]
                        for r in conn.execute(
                            "SELECT target_id FROM ratings WHERE profile_id = ? AND target = 'note'",
                            (pid,),
                        ).fetchall()
                    ]
                    conn.execute("DELETE FROM state WHERE profile_id = ?", (pid,))
                    conn.execute("DELETE FROM ratings WHERE profile_id = ?", (pid,))
                    conn.execute("DELETE FROM notes WHERE profile_id = ?", (pid,))
                    conn.execute("DELETE FROM profiles WHERE id = ?", (pid,))
                    # пересчёт агрегатов после удаления оценок
                    for tid in affected_ch:
                        conn.execute(
                            """UPDATE chapters SET rating =
                                COALESCE((SELECT SUM(value) FROM ratings
                                          WHERE target = 'chapter' AND target_id = ?), 0)
                                WHERE id = ?""",
                            (tid, tid),
                        )
                    for tid in affected_n:
                        conn.execute(
                            """UPDATE notes SET rating =
                                COALESCE((SELECT SUM(value) FROM ratings
                                          WHERE target = 'note' AND target_id = ?), 0)
                                WHERE id = ?""",
                            (tid, tid),
                        )
                    conn.commit()
                    if session.get("profile_id") == pid:
                        session["profile_id"] = db.default_profile_id()
                    flash("Профиль удалён: прогресс, заметки и оценки очищены")
            elif action == "reset" and pid_in:
                conn.execute(
                    "DELETE FROM state WHERE profile_id = ?", (int(pid_in),)
                )
                conn.commit()
                flash("Прогресс чтения сброшен")
        finally:
            conn.close()
        return redirect(url_for("profiles_page"))

    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT p.id, p.name,
                      (SELECT COUNT(*) FROM state s WHERE s.profile_id = p.id
                           AND s.done > 0) chapters_done,
                      (SELECT COUNT(*) FROM notes n WHERE n.profile_id = p.id) notes_count
               FROM profiles p ORDER BY p.id"""
        ).fetchall()
    finally:
        conn.close()
    return render_template("profiles.html", rows=rows)


# ---------------------------------------------------------------- D4: передача библиотеки

@app.get("/api/library.zip")
def library_export():
    raw = request.args.get("ids", "")
    ids = [int(x) for x in re.split(r"[,\s]+", raw) if x.isdigit()][:100]
    if not ids:
        flash("Отметьте книги для экспорта")
        return redirect(url_for("feed", t="library"))
    import io
    import zipfile

    conn = get_db()
    try:
        books = conn.execute(
            f"SELECT * FROM books WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
            ids,
        ).fetchall()
        chapters_by_book = {}
        for b in books:
            chapters_by_book[b["id"]] = conn.execute(
                "SELECT ord, title, html, words FROM chapters WHERE book_id = ? ORDER BY ord",
                (b["id"],),
            ).fetchall()
    finally:
        conn.close()

    manifest = {"format": "pikabureader-library", "version": 1, "books": []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for b in books:
            entry = {
                "title": b["title"], "author": b["author"], "tags": b["tags"],
                "description": b["description"], "cover": "",
                "chapters": [dict(c) for c in chapters_by_book[b["id"]]],
            }
            if b["cover"]:
                cover_path = os.path.join(COVER_DIR, b["cover"])
                if os.path.exists(cover_path):
                    entry["cover"] = b["cover"]
                    zf.write(cover_path, f"covers/{b['cover']}")
            img_dir = os.path.join(IMG_DIR, f"book_{b['id']}")
            if os.path.isdir(img_dir):
                for f in sorted(os.listdir(img_dir)):
                    p = os.path.join(img_dir, f)
                    if os.path.isfile(p):
                        zf.write(p, f"images/{b['id']}/{f}")
            manifest["books"].append(entry)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    buf.seek(0)
    _log_import("export", f"{len(books)} книг ({', '.join(b['title'] for b in books)})")
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name="pikabu-library.zip",
    )


@app.post("/api/library/import")
def library_import():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Файл не выбран")
        return redirect(url_for("add"))
    import zipfile

    try:
        zf = zipfile.ZipFile(file)
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != "pikabureader-library":
            raise ValueError("не тот формат архива")
    except Exception as exc:  # noqa: BLE001
        flash(f"Не удалось прочитать архив: {exc}")
        return redirect(url_for("add"))

    imported, skipped = 0, 0
    try:
        conn = get_db()
        try:
            for b in manifest.get("books", [])[:100]:
                title = (b.get("title") or "").strip()
                if not title:
                    continue
                dup = conn.execute(
                    "SELECT id FROM books WHERE lower(title) = lower(?) AND lower(author) = lower(?)",
                    (title, (b.get("author") or "").strip()),
                ).fetchone()
                if dup:
                    skipped += 1
                    continue
                cur = conn.execute(
                    """INSERT INTO books(title, author, tags, description, created_at)
                       VALUES(?,?,?,?,?)""",
                    (title, (b.get("author") or "").strip(),
                     (b.get("tags") or "").strip()[:200],
                     (b.get("description") or "")[:2000], db.now()),
                )
                book_id = cur.lastrowid

                # обложка
                cover = (b.get("cover") or "").strip()
                if cover:
                    try:
                        data = zf.read(f"covers/{cover}")
                    except KeyError:
                        data = b""
                    if data:
                        ext = os.path.splitext(cover)[1] or ".jpg"
                        cover_name = f"book_{book_id}{ext}"
                        os.makedirs(COVER_DIR, exist_ok=True)
                        with open(os.path.join(COVER_DIR, cover_name), "wb") as f:
                            f.write(data)
                        conn.execute(
                            "UPDATE books SET cover = ? WHERE id = ?",
                            (cover_name, book_id),
                        )

                # картинки глав: ремап путей под новый book_id
                names = [
                    n for n in zf.namelist()
                    if n.startswith(f"images/{b.get('id', -1)}/")
                ]
                if names:
                    folder = os.path.join(IMG_DIR, f"book_{book_id}")
                    os.makedirs(folder, exist_ok=True)
                    for n in names:
                        with open(os.path.join(folder, os.path.basename(n)), "wb") as f:
                            f.write(zf.read(n))

                for ch in b.get("chapters", [])[:1000]:
                    html = re.sub(
                        r"/media/images/\d+/", f"/media/images/{book_id}/",
                        ch.get("html") or "",
                    )
                    text = re.sub(r"<[^>]+>", "", html)
                    ctitle = (ch.get("title") or "Глава")[:300]
                    cur = conn.execute(
                        """INSERT INTO chapters(book_id, ord, title, html, words, created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (book_id, int(ch.get("ord") or 1), ctitle, html,
                         len(text.split()), db.now()),
                    )
                    try:
                        conn.execute(
                            "INSERT INTO chapters_fts(rowid, title, content) VALUES (?,?,?)",
                            (cur.lastrowid, ctitle, db.strip_html(html)),
                        )
                    except sqlite3.OperationalError:
                        pass
                imported += 1
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        flash(f"Ошибка импорта: {exc}")
        return redirect(url_for("add"))
    finally:
        zf.close()

    n_ch = sum(len(b.get("chapters", [])) for b in manifest.get("books", []))
    _log_import(
        "library-import",
        f"импортировано {imported}, пропущено дублей {skipped}, глав {n_ch}",
    )
    if imported:
        flash(f"Импортировано книг: {imported} (пропущено дублей: {skipped})")
    else:
        flash("Новых книг не найдено (все уже в библиотеке)")
    return redirect(url_for("feed", t="library"))


# ---------------------------------------------------------------- D10: панель владельца

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "set_password":
            _set_password((request.form.get("password") or "").strip())
            session["owner"] = 1
            pw = (request.form.get("password") or "").strip()
            flash("Пароль обновлён" if pw else "Пароль снят — инстанс открыт")
        elif action == "reindex":
            count = db.reindex_fts()
            flash(f"FTS переиндексирован: {count} глав")
        return redirect(url_for("admin"))

    log_lines: list[str] = []
    if os.path.exists(IMPORT_LOG):
        try:
            with open(IMPORT_LOG, encoding="utf-8") as f:
                log_lines = f.readlines()[-50:][::-1]
        except OSError:
            pass
    password_set = bool(db.get_settings().get("password_hash", ""))
    return render_template(
        "admin.html", log_lines=log_lines, password_set=password_set,
        data_size=_fmt_size(_data_size()), schema_version=db.schema_version(),
    )


# ---------------------------------------------------------------- init

db.init_db()
for d in (UPLOAD_DIR, IMG_DIR, COVER_DIR):
    os.makedirs(d, exist_ok=True)

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
