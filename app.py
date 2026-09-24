import json
import os
import re
import sqlite3
import uuid
from functools import wraps
from html import escape as _escape

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, send_from_directory, url_for)

import db
from parsers import parse_book

__version__ = "0.2.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
IMG_DIR = os.path.join(db.DATA_DIR, "images")
COVER_DIR = os.path.join(db.DATA_DIR, "covers")

app = Flask(__name__)
app.secret_key = "pikabureader-local"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


def get_db() -> sqlite3.Connection:
    return db.connect()


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
    try:
        counts = {
            "feed": conn.execute("SELECT COUNT(*) c FROM chapters").fetchone()["c"],
            "books": conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"],
            "bookmarks": conn.execute(
                "SELECT COUNT(*) c FROM state WHERE bookmark = 1"
            ).fetchone()["c"],
            "reading": conn.execute(
                "SELECT COUNT(*) c FROM state WHERE done = 0 AND read_pct > 0"
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
               WHERE s.done = 0 AND s.read_pct > 0
               ORDER BY s.last_read_at DESC LIMIT 5"""
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
               (SELECT COUNT(*) FROM notes n WHERE n.chapter_id = c.id) note_count
        FROM chapters c JOIN books b ON b.id = c.book_id
        LEFT JOIN state s ON s.chapter_id = c.id
        {where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, (*params, per_page, offset)).fetchall()
    count_sql = f"SELECT COUNT(*) c FROM chapters c JOIN books b ON b.id = c.book_id LEFT JOIN state s ON s.chapter_id = c.id {where}"
    total = conn.execute(count_sql, params).fetchone()["c"]
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
               WHERE s.bookmark = 1 ORDER BY s.last_read_at DESC"""
        ).fetchall()
    finally:
        conn.close()
    return render_template("list.html", rows=rows, heading="Закладки")


# ---------------------------------------------------------------- book

@app.route("/book/<int:book_id>")
def book_page(book_id: int):
    conn = get_db()
    try:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            abort(404)
        rows = conn.execute(
            """SELECT c.*, s.read_pct, s.bookmark, s.done
               FROM chapters c LEFT JOIN state s ON s.chapter_id = c.id
               WHERE c.book_id = ? ORDER BY c.ord""",
            (book_id,),
        ).fetchall()
        progress = conn.execute(
            """SELECT COALESCE(SUM(s.done), 0) done, COUNT(*) total
               FROM chapters c LEFT JOIN state s ON s.chapter_id = c.id
               WHERE c.book_id = ?""",
            (book_id,),
        ).fetchone()
        # M9: куда вести «Продолжить чтение»
        resume = conn.execute(
            """SELECT c.id FROM chapters c
               JOIN state s ON s.chapter_id = c.id
               WHERE c.book_id = ? AND s.done = 0 AND s.read_pct > 0
               ORDER BY s.last_read_at DESC LIMIT 1""",
            (book_id,),
        ).fetchone()
        resume_id = resume["id"] if resume else None
        resume_label = "Продолжить чтение" if resume else ""
        if resume_id is None:
            nxt_unread = conn.execute(
                """SELECT c.id FROM chapters c
                   LEFT JOIN state s ON s.chapter_id = c.id
                   WHERE c.book_id = ? AND COALESCE(s.done, 0) = 0
                   ORDER BY c.ord LIMIT 1""",
                (book_id,),
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
    try:
        ch = conn.execute(
            """SELECT c.*, b.title book_title, b.author, b.cover, b.id bid,
                      s.read_pct, s.bookmark, s.done
               FROM chapters c JOIN books b ON b.id = c.book_id
               LEFT JOIN state s ON s.chapter_id = c.id
               WHERE c.id = ?""",
            (chapter_id,),
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
            "SELECT * FROM notes WHERE chapter_id = ? ORDER BY id",
            (chapter_id,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) c FROM chapters WHERE book_id = ?", (ch["bid"],)
        ).fetchone()["c"]
    finally:
        conn.close()

    # build note tree
    tree: dict = {None: []}
    nodes: dict = {}
    for n in notes:
        node = {"id": n["id"], "text": n["text"], "rating": n["rating"],
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
        book_pos=ch["ord"], book_total=total,
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
    conn = get_db()
    try:
        table = "chapters" if target == "chapter" else "notes"
        conn.execute(f"UPDATE {table} SET rating = rating + ? WHERE id = ?", (delta, tid))
        conn.commit()
        row = conn.execute(f"SELECT rating FROM {table} WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("not found")
    return {"ok": True, "rating": row["rating"]}


@app.post("/api/progress")
@json_api
def api_progress():
    payload = request.get_json(force=True)
    cid = int(payload.get("chapter_id", 0))
    pct_ = max(0, min(100, int(payload.get("pct", 0))))
    done = 1 if payload.get("done") else 0
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO state(chapter_id, read_pct, done, last_read_at)
               VALUES(?,?,?,?)
               ON CONFLICT(chapter_id) DO UPDATE SET
                 read_pct = MAX(state.read_pct, excluded.read_pct),
                 done = MAX(state.done, excluded.done),
                 last_read_at = excluded.last_read_at""",
            (cid, pct_, done, db.now()),
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
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO state(chapter_id, bookmark, last_read_at)
               VALUES(?,1,?) ON CONFLICT(chapter_id)
               DO UPDATE SET bookmark = 1 - state.bookmark, last_read_at = excluded.last_read_at""",
            (cid, db.now()),
        )
        conn.commit()
        row = conn.execute("SELECT bookmark FROM state WHERE chapter_id = ?", (cid,)).fetchone()
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
    if not text or cid <= 0:
        raise ValueError("empty note")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO notes(chapter_id, parent_id, text, created_at) VALUES(?,?,?,?)",
            (cid, int(parent) if parent else None, text, db.now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
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
        conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
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


# ---------------------------------------------------------------- init

db.init_db()
for d in (UPLOAD_DIR, IMG_DIR, COVER_DIR):
    os.makedirs(d, exist_ok=True)

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
