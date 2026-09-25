import io
import os
import re
import urllib.parse
import zipfile
from datetime import date, timedelta

import pytest

import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Тестовое приложение с базой во временном каталоге."""
    import app as app_module

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "IMG_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(app_module, "COVER_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(app_module, "IMPORT_LOG", str(tmp_path / "import.log"))
    db.init_db()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def seed():
    """Книга с одной главой; возвращает id главы."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO books(title, author, created_at) VALUES('Тест', 'Автор', '2026-01-01 00:00:00')"
        )
        bid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO chapters(book_id, ord, title, html, words, created_at) "
            "VALUES(?, 1, 'Глава 1', '<p>Текст главы для чтения</p>', 4, '2026-01-01 00:00:00')",
            (bid,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------- R1/R2

def test_note_with_quote(client):
    cid = seed()
    r = client.post("/api/note", json={"chapter_id": cid, "text": "мысль", "quote": "Текст главы"})
    assert r.status_code == 200
    note = r.get_json()["note"]
    assert note["quote"] == "Текст главы"
    # цитата отражена на странице главы
    page = client.get(f"/story/{cid}").get_data(as_text=True)
    assert "note-quote" in page and "Текст главы" in page


def test_note_edit_sets_edited_at(client):
    cid = seed()
    note = client.post("/api/note", json={"chapter_id": cid, "text": "старый"}).get_json()["note"]
    assert note["edited_at"] == ""
    r = client.post("/api/note/edit", json={"id": note["id"], "text": "новый"})
    assert r.status_code == 200
    updated = r.get_json()["note"]
    assert updated["text"] == "новый"
    assert updated["edited_at"] != ""


def test_note_edit_not_found(client):
    r = client.post("/api/note/edit", json={"id": 999, "text": "x"})
    assert r.status_code == 400


def test_note_empty_rejected(client):
    cid = seed()
    r = client.post("/api/note", json={"chapter_id": cid, "text": "  "})
    assert r.status_code == 400


# ---------------------------------------------------------------- R6

def test_api_chapter_full_text(client):
    cid = seed()
    r = client.post("/api/chapter", json={"chapter_id": cid})
    assert r.status_code == 200
    res = r.get_json()
    assert res["ok"] and "Текст главы для чтения" in res["html"]


def test_api_chapter_bad_payload(client):
    cid = seed()
    assert client.post("/api/chapter", json={}).status_code == 400
    assert client.post("/api/chapter", json={"chapter_id": 0}).status_code == 400
    assert (
        client.post("/api/chapter", json={"chapter_id": cid + 9999}).status_code
        == 400
    )


# ---------------------------------------------------------------- N7: недавно читал

def seed_history(profile_id=1, n=22):
    """n глав с read-состоянием: глава n — самая свежая по last_read_at."""
    conn = db.connect()
    try:
        for i in range(1, n + 1):
            cur = conn.execute(
                "INSERT INTO books(title, created_at) VALUES(?, '2026-01-01 00:00:00')",
                (f"Книга {i}",),
            )
            cur = conn.execute(
                "INSERT INTO chapters(book_id, ord, title, html, words, created_at) "
                "VALUES(?, 1, ?, ?, 4, '2026-01-01 00:00:00')",
                (cur.lastrowid, f"Глава {i}", f"<p>Текст {i}</p>"),
            )
            conn.execute(
                "INSERT INTO state(chapter_id, profile_id, read_pct, last_read_at) "
                "VALUES(?, ?, 50, ?)",
                (cur.lastrowid, profile_id, f"2026-01-{i:02d} 12:00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def test_history_recent_20_desc(client):
    seed_history()
    r = client.get("/history")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Недавно читал" in html
    assert html.count('class="story-card"') == 20
    # свежие первыми (22 раньше 3), самые старые (1, 2) отброшены
    assert html.index("Глава 22") < html.index("Глава 3")
    assert ">Глава 1<" not in html


def test_history_empty(client):
    html = client.get("/history").get_data(as_text=True)
    assert "Ещё ничего не читали." in html


def test_history_profile_isolation(client):
    seed_history(profile_id=2, n=3)
    html = client.get("/history").get_data(as_text=True)
    assert "Ещё ничего не читали." in html


# ---------------------------------------------------------------- N3

def test_story_has_toc(client):
    cid = seed()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO chapters(book_id, ord, title, html, words, created_at) "
            "SELECT book_id, 2, 'Глава 2', '<p>вторая</p>', 2, '2026-01-01 00:00:00' "
            "FROM chapters WHERE id = ?",
            (cid,),
        )
        conn.commit()
    finally:
        conn.close()
    page = client.get(f"/story/{cid}").get_data(as_text=True)
    assert "toc-pop" in page
    assert "Глава 1" in page and "Глава 2" in page


# ---------------------------------------------------------------- M4

def test_settings_page(client):
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Бэкап" in body and "Скачать бэкап" in body


def test_backup_zip_roundtrip(client):
    seed()
    r = client.get("/api/backup.zip")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        assert "app.db" in zf.namelist()

    # удаляем книгу и восстанавливаемся из бэкапа
    conn = db.connect()
    try:
        conn.execute("DELETE FROM books")
        conn.commit()
    finally:
        conn.close()
    assert client.get("/").get_data(as_text=True).find("Здесь пока пусто") != -1

    data = r.data
    r2 = client.post(
        "/api/backup/restore",
        data={"file": (io.BytesIO(data), "backup.zip")},
        content_type="multipart/form-data",
    )
    assert r2.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Тест" in page


def test_restore_rejects_garbage(client):
    cid = seed()
    # zip без app.db — не бэкап, данные не трогаем
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    buf.seek(0)
    r = client.post(
        "/api/backup/restore",
        data={"file": (io.BytesIO(buf.getvalue()), "not-backup.zip")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302  # redirect на настройки с flash
    # книга на месте
    page = client.get(f"/story/{cid}").get_data(as_text=True)
    assert "Глава 1" in page


def test_restore_rejects_non_zip(client):
    r = client.post(
        "/api/backup/restore",
        data={"file": (io.BytesIO(b"not a zip"), "x.txt")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302


# ---------------------------------------------------------------- D3: пароль

def test_password_gate(client):
    # пароль не задан — доступ открыт
    assert client.get("/").status_code == 200
    # задаём пароль через панель (сразу логинит)
    r = client.post(
        "/admin", data={"action": "set_password", "password": "secret"}
    )
    assert r.status_code == 302
    client.get("/logout")
    # теперь лента требует входа
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    # неверный пароль — остаёмся на форме
    r = client.post("/login", data={"password": "wrong"})
    assert r.status_code == 200 and "Неверный пароль" in r.get_data(as_text=True)
    # верный пароль — редирект и доступ
    r = client.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/").status_code == 200
    # выход → API отвечает 401
    client.get("/logout")
    r = client.get("/api/settings")
    assert r.status_code == 401
    assert r.get_json()["error"] == "unauthorized"


def test_login_open_when_no_password(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["version"]


# ---------------------------------------------------------------- D2: профили

def test_profiles_isolate_progress(client):
    cid = seed()
    # профиль по умолчанию читает и пишет заметку
    client.post("/api/progress", json={"chapter_id": cid, "pct": 50, "done": 0})
    client.post("/api/note", json={"chapter_id": cid, "text": "заметка первого"})
    page = client.get(f"/story/{cid}").get_data(as_text=True)
    assert 'data-read-pct="50"' in page
    assert "заметка первого" in page

    # создаём второго читателя и переключаемся
    client.post("/profiles", data={"action": "create", "name": "Второй"})
    conn = db.connect()
    try:
        pid2 = conn.execute(
            "SELECT id FROM profiles WHERE name = 'Второй'"
        ).fetchone()["id"]
    finally:
        conn.close()
    client.post("/profiles", data={"action": "select", "id": str(pid2)})
    # у второго свой прогресс и свои заметки
    page = client.get(f"/story/{cid}").get_data(as_text=True)
    assert 'data-read-pct="0"' in page
    assert "заметка первого" not in page
    # вернулись к первому — всё на месте
    client.post("/profiles", data={"action": "select", "id": "1"})
    page = client.get(f"/story/{cid}").get_data(as_text=True)
    assert 'data-read-pct="50"' in page
    assert "заметка первого" in page


def test_profiles_create_rename_delete(client):
    client.post("/profiles", data={"action": "create", "name": "Мама"})
    client.post("/profiles", data={"action": "create", "name": "Мама"})  # дубль
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
        pid = conn.execute("SELECT id FROM profiles WHERE name = 'Мама'").fetchone()["id"]
    finally:
        conn.close()
    assert n == 2  # дубль не создан
    client.post("/profiles", data={"action": "rename", "id": str(pid), "name": "Папа"})
    # последний профиль удалить нельзя (сначала тест reset на Мама/Папа)
    client.post("/profiles", data={"action": "delete", "id": str(pid)})
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
        names = [r["name"] for r in conn.execute("SELECT name FROM profiles")]
    finally:
        conn.close()
    assert n == 1 and "Папа" not in names
    # нельзя удалить последний
    client.post("/profiles", data={"action": "delete", "id": "1"})
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_ratings_one_vote_per_profile(client):
    cid = seed()
    r = client.post("/api/rate", json={"target": "chapter", "id": cid, "delta": 1})
    assert r.get_json()["rating"] == 1 and r.get_json()["mine"] == 1
    # повторный плюс не задваивает
    r = client.post("/api/rate", json={"target": "chapter", "id": cid, "delta": 1})
    assert r.get_json()["rating"] == 1 and r.get_json()["mine"] == 1
    # минус забирает голос обратно
    r = client.post("/api/rate", json={"target": "chapter", "id": cid, "delta": -1})
    assert r.get_json()["rating"] == 0 and r.get_json()["mine"] == 0


# ---------------------------------------------------------------- D4: передача библиотеки

def test_library_share_roundtrip(client):
    seed()
    r = client.get("/api/library.zip?ids=1")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        manifest = __import__("json").loads(zf.read("manifest.json"))
    assert manifest["format"] == "pikabureader-library"
    assert manifest["books"][0]["title"] == "Тест"
    exported = r.data

    # удаляем локально и импортируем от «друга»
    conn = db.connect()
    try:
        conn.execute("DELETE FROM books")
        conn.commit()
    finally:
        conn.close()
    r = client.post(
        "/api/library/import",
        data={"file": (io.BytesIO(exported), "lib.zip")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    # id главы мог измениться — смотрим ленту
    feed = client.get("/?q=Тест").get_data(as_text=True)
    assert "Тест" in feed
    # повторный импорт — дубли не создаются
    r = client.post(
        "/api/library/import",
        data={"file": (io.BytesIO(exported), "lib.zip")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_library_export_requires_selection(client):
    r = client.get("/api/library.zip")
    assert r.status_code == 302  # flash «Отметьте книги»


def test_import_log_written(client):
    import os

    seed()
    client.get("/api/library.zip?ids=1")
    import app as app_module
    assert os.path.exists(app_module.IMPORT_LOG)
    with open(app_module.IMPORT_LOG, encoding="utf-8") as f:
        content = f.read()
    assert "export" in content and "Тест" in content


# ---------------------------------------------------------------- helpers (волна 6)

NS_FB21 = "http://www.gribuser.ru/xml/fb2.1"


def fb2_bytes(title: str, sections) -> bytes:
    """Минимальный валидный FB2: sections = [(заголовок, текст)] или [текст]."""
    body = ""
    for s in sections:
        if isinstance(s, tuple):
            body += f"<section><title><p>{s[0]}</p></title><p>{s[1]}</p></section>"
        else:
            body += f"<section><p>{s}</p></section>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<FictionBook xmlns="{NS_FB21}">'
        "<description><title-info>"
        f"<book-title>{title}</book-title>"
        "<author><first-name>Иван</first-name><last-name>Петров</last-name></author>"
        "<genre>prose</genre>"
        "</title-info></description>"
        f"<body>{body}</body></FictionBook>"
    ).encode("utf-8")


def main_content(page: str) -> str:
    """Только центральная колонка — без книг сайдбаров (для проверок ленты)."""
    if '<main class="content">' not in page:
        return page
    return page.split('<main class="content">', 1)[1].split("</main>", 1)[0]


def add_book_direct(title, tags="", author="Автор"):
    """Книга с одной главой напрямую в БД; возвращает id главы."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO books(title, author, tags, created_at) VALUES(?, ?, ?, '2026-01-01')",
            (title, author, tags),
        )
        cur = conn.execute(
            "INSERT INTO chapters(book_id, ord, title, html, words, created_at) "
            "VALUES(?, 1, 'Глава', '<p>текст</p>', 2, '2026-01-01')",
            (cur.lastrowid,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------- F3/F5: импорт

def test_add_multiple_files_at_once(client):
    """F3: несколько файлов в одной загрузке — каждый в свою карточку."""
    data = {
        "file": [
            (io.BytesIO(fb2_bytes("Книга А", ["Первый раздел"])), "a.fb2"),
            (io.BytesIO(fb2_bytes("Книга Б", ["Второй раздел"])), "b.fb2"),
        ],
    }
    r = client.post("/add", data=data, content_type="multipart/form-data",
                    follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Результаты импорта" in page
    assert "Книга А" in page and "Книга Б" in page
    assert page.count("import-badge-ok") == 2
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"]
    finally:
        conn.close()
    assert n == 2


def test_add_error_shows_card(client):
    """F5: ошибка импорта — понятная карточка, а не мелкий flash."""
    r = client.post(
        "/add",
        data={"file": (io.BytesIO(b"plain text"), "note.docx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    page = r.get_data(as_text=True)
    assert "Результаты импорта" in page
    assert "Формат не поддерживается" in page
    assert "EPUB, FB2" in page  # подсказка


def test_add_duplicate_reports_card(client):
    """F4/F5: дубликат — карточка со ссылкой на существующую книгу."""
    add_book_direct("Книга А", author="Иван Петров")
    r = client.post(
        "/add",
        data={"file": (io.BytesIO(fb2_bytes("Книга А", ["Раздел"])), "a.fb2")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    page = r.get_data(as_text=True)
    assert "Уже в библиотеке" in page
    assert "Открыть существующую" in page


def test_add_txt_markdown_import(client):
    """F6: .txt/.md импортируются, главы по заголовкам # / ##."""
    content = "# Раздел первый\nПривет, текст-абзац.\n\n## Раздел второй\nЕщё текст."
    r = client.post(
        "/add",
        data={"file": (io.BytesIO(content.encode("utf-8")), "заметки.md")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    page = r.get_data(as_text=True)
    assert "Результаты импорта" in page
    assert "import-badge-ok" in page

    conn = db.connect()
    try:
        row = conn.execute("SELECT id, title FROM books").fetchone()
    finally:
        conn.close()
    assert row is not None and row["title"] == "заметки"
    book = client.get(f"/book/{row['id']}").get_data(as_text=True)
    assert "Раздел первый" in book and "Раздел второй" in book


def test_add_empty_txt_shows_card(client):
    """F6/F5: пустой txt — понятная карточка «Пустой файл»."""
    r = client.post(
        "/add",
        data={"file": (io.BytesIO("   \n\n".encode()), "empty.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    page = r.get_data(as_text=True)
    assert "Пустой файл" in page


# ---------------------------------------------------------------- F9: предпросмотр

def test_import_preview_flow(client):
    """F9: предпросмотр → смена режима → импорт."""
    fb = fb2_bytes("Большая книга", [("Раздел", "Слово " * 700)])
    r = client.post(
        "/add?preview=1",
        data={"file": (io.BytesIO(fb), "big.fb2"), "preview": "1"},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302 and "/add/preview" in r.headers["Location"]
    page = client.get("/add/preview?m=parts").get_data(as_text=True)
    assert "Большая книга" in page and "Будет" in page
    page_compact = client.get("/add/preview?m=compact").get_data(as_text=True)
    assert "Компактные" in page_compact
    # импорт выбранным режимом
    r = client.post("/add/import", data={"mode": "compact"}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Добавлена" in page
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM chapters").fetchone()["c"]
    finally:
        conn.close()
    assert n >= 2  # compact разрезал длинную главу


def test_split_mode_helpers():
    """F9: юнит-проверка режимов разбивки."""
    import app as app_module

    long_html = "<p>" + ("Абзац текста. " * 200) + "</p>"
    parts = app_module._apply_split_mode([("Раздел", long_html)], "compact")
    assert len(parts) > 1
    assert all("Раздел" in t for t, _ in parts)
    # исходный режим не трогает ничего
    assert app_module._apply_split_mode([("Раздел", long_html)], "parts") == [("Раздел", long_html)]
    # merge склеивает мелкие секции
    small = [("Секция " + str(i), "<p>короткий текст</p>") for i in range(8)]
    merged = app_module._apply_split_mode(small, "merge")
    assert len(merged) < len(small)
    assert sum(h.count("<hr>") for _, h in merged) == len(small) - len(merged)


def test_preview_without_file_redirects(client):
    r = client.get("/add/preview")
    assert r.status_code == 302


# ---------------------------------------------------------------- N4: теги

def test_feed_tag_filter(client):
    add_book_direct("Фентези-книга", tags="фентези, приключения")
    add_book_direct("Детектив-книга", tags="детектив")
    content = main_content(client.get("/?tag=фентези").get_data(as_text=True))
    assert "Фентези-книга" in content
    assert "Детектив-книга" not in content
    # теги в ленте — ссылки на фильтр
    content = main_content(client.get("/").get_data(as_text=True))
    assert "tag=" in content and "фентези" in content


def test_feed_tag_filter_empty(client):
    add_book_direct("Детектив-книга", tags="детектив")
    content = main_content(client.get("/?tag=фентези").get_data(as_text=True))
    assert "Детектив-книга" not in content
    assert "Здесь пока пусто" in content


# ---------------------------------------------------------------- N5: библиотека

def test_library_sorting(client):
    add_book_direct("Альфа")   # id меньше → в new последняя
    add_book_direct("Бета")    # id больше → в new первая
    content = main_content(client.get("/?t=library&sort=title").get_data(as_text=True))
    assert content.index("Альфа") < content.index("Бета")
    content = main_content(client.get("/?t=library&sort=new").get_data(as_text=True))
    assert content.index("Бета") < content.index("Альфа")
    content = main_content(client.get("/?t=library&sort=author").get_data(as_text=True))
    assert "Сначала новые" in content  # активная вкладка есть


def test_library_sort_by_progress(client):
    aid = add_book_direct("Альфа")
    add_book_direct("Бета")
    # «Альфа» полностью прочитана
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO state(chapter_id, profile_id, done, read_pct) VALUES(?, 1, 1, 100)",
            (aid,),
        )
        conn.commit()
    finally:
        conn.close()
    content = main_content(client.get("/?t=library&sort=progress").get_data(as_text=True))
    assert content.index("Альфа") < content.index("Бета")


# ---------------------------------------------------------------- N6: закладка с цитатой

def test_bookmark_saves_quote(client):
    cid = seed()
    r = client.post("/api/bookmark", json={"chapter_id": cid, "quote": "Текст главы для чтения"})
    assert r.get_json()["bookmark"] is True
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT bookmark_quote FROM state WHERE chapter_id = ?", (cid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["bookmark_quote"] == "Текст главы для чтения"
    # цитата видна на странице закладок
    page = client.get("/bookmarks").get_data(as_text=True)
    assert "bookmark-quote" in page and "Текст главы для чтения" in page
    # выключение — цитата очищается
    client.post("/api/bookmark", json={"chapter_id": cid})
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT bookmark, bookmark_quote FROM state WHERE chapter_id = ?", (cid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["bookmark"] == 0 and row["bookmark_quote"] == ""


# ---------------------------------------------------------------- M5: экспорт заметок

def test_notes_export_markdown(client):
    cid = seed()
    client.post("/api/note", json={"chapter_id": cid, "text": "важная мысль", "quote": "цитата-основание"})
    r = client.get("/notes/export")
    assert r.mimetype.startswith("text/markdown")
    body = r.get_data(as_text=True)
    assert "важная мысль" in body
    assert "цитата-основание" in body
    assert "## " in body and "### Глава 1" in body
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_notes_export_empty(client):
    r = client.get("/notes/export")
    assert r.status_code == 200
    assert "Заметок пока нет" in r.get_data(as_text=True)


# ---------------------------------------------------------------- Q5: контраст

def _css_theme_blocks():
    import pathlib
    import re as _re

    css = pathlib.Path("static/css/pikabu.css").read_text(encoding="utf-8")

    def block(pattern):
        m = _re.search(pattern + r"\s*\{(.*?)\}", css, _re.S)
        assert m, pattern
        return dict(_re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))

    root = block(r":root")
    dark = {**root, **block(r'\[data-theme="dark"\]')}
    return root, dark


def _rel_luminance(hex_color: str) -> float:
    vals = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_theme_contrast_wcag_aa():
    """Q5: основные пары цветов обеих тем — не ниже 4.5:1 (WCAG AA)."""
    root, dark = _css_theme_blocks()
    for name, theme in (("light", root), ("dark", dark)):
        for fg in ("--text", "--text-2", "--link", "--green", "--red"):
            for bg in ("--bg", "--card"):
                ratio = _contrast(theme[fg], theme[bg])
                assert ratio >= 4.5, f"{name}: {fg} на {bg} = {ratio:.2f}"
        # тёмный текст на жёлтом акценте (кнопки/активные вкладки)
        assert _contrast("#1a1a1a", theme["--accent"]) >= 4.5


# ---------------- UI: hidden должен побеждать авторские display

def test_hidden_attribute_overrides_display():
    """Панель оглавления (.toc-pop, display:flex) обязана скрываться по hidden."""
    import pathlib
    import re as _re

    css = pathlib.Path("static/css/pikabu.css").read_text(encoding="utf-8")
    assert _re.search(
        r"\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;?\s*\}", css
    ), "нет глобального правила [hidden] { display: none !important }"
    m = _re.search(r"\.toc-pop\s*\{(.*?)\}", css, _re.S)
    assert m and "display: flex" in m.group(1)


# ---------------- X2: внутрикнижные ссылки (сноски) → /goto

def test_rewrite_local_links_to_goto():
    """Ссылки между файлами EPUB ведут на /goto, мёртвые — снимаются."""
    import app as app_module

    srcs = {"Text/ch.xhtml", "Text/notes.xhtml", "Text/g1.xhtml"}
    h = (
        '<p><a href="../Text/notes.xhtml#vv-1">1</a>'
        ' <a href="g1.xhtml#g1-1">2</a>'
        ' <a href="#same">3</a>'
        ' <a href="http://example.com">4</a>'
        ' <a href="../Text/missing.xhtml#z">5</a></p>'
    )
    out = app_module._rewrite_local_links(h, "Text/ch.xhtml", srcs, 7)
    assert 'href="/goto/7/Text/notes.xhtml?frag=vv-1"' in out
    assert 'href="/goto/7/Text/g1.xhtml?frag=g1-1"' in out
    assert 'href="/goto/7/Text/ch.xhtml?frag=same"' in out
    assert 'href="http://example.com"' in out
    assert "missing.xhtml" not in out  # не импортирован — ссылка снята
    assert " 5</p>" in out  # текст ссылки остался
    # FB2/txt: файлов-источников нет — ничего не трогаем
    assert app_module._rewrite_local_links(h, "", srcs, 7) == h


def test_split_mode_keeps_sources_aligned():
    """F9-режимы возвращают sources, выровненные с главами."""
    import app as app_module

    ch = [("A", "<p>текст главы а</p>"), ("B", "<p>текст главы б</p>")]
    src = ["x/a.xhtml", "x/b.xhtml"]

    out, s = app_module._apply_split_mode(ch, "parts", src)
    assert out == ch and s == src

    out, s = app_module._apply_split_mode(ch, "merge", src)
    assert len(out) == 1 and s == ["x/a.xhtml"]

    long_html = "<p>" + ("слово " * 900) + "</p>"
    out, s = app_module._apply_split_mode([("A", long_html)], "compact", ["x/a.xhtml"])
    assert len(out) > 1 and set(s) == {"x/a.xhtml"}

    # без sources — прежнее поведение (обычный список)
    assert app_module._apply_split_mode(ch, "parts") == ch


def test_store_book_rewrites_links_and_keeps_source(client):
    """Импорт EPUB-подобной книги: href переписан, source сохранён."""
    import app as app_module
    from parsers import ParsedBook

    parsed = ParsedBook(title="Книга", author="Автор")
    parsed.chapters = [
        ("Глава", '<p><a href="../Text/notes.xhtml#vv-1">сноска</a></p>'),
        ("Примечания", '<p><span id="vv-1">Примечание</span></p>'),
    ]
    parsed.sources = ["Text/ch.xhtml", "Text/notes.xhtml"]
    res = app_module._store_book(parsed, parsed.chapters)
    assert res["status"] == "ok"
    bid = res["book_id"]

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ord, html, source FROM chapters WHERE book_id = ? ORDER BY ord",
            (bid,),
        ).fetchall()
    finally:
        conn.close()
    assert rows[0]["source"] == "Text/ch.xhtml"
    assert rows[1]["source"] == "Text/notes.xhtml"
    assert f"/goto/{bid}/Text/notes.xhtml?frag=vv-1" in rows[0]["html"]


def test_goto_route(client):
    """/goto ведёт на главу с якорем, на несуществующее — не даёт 404."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO books(title, author, created_at) VALUES('К','А','x')"
        )
        bid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO chapters(book_id, ord, title, html, source, created_at) "
            "VALUES(?, 1, 'Глава', '<p>текст</p>', 'Text/ch1.xhtml', 'x')",
            (bid,),
        )
        ch1 = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO chapters(book_id, ord, title, html, source, created_at) "
            "VALUES(?, 2, 'Сноски', '<p><span id=\"vv-1\">Примечание</span></p>', "
            "'Text/notes.xhtml', 'x')",
            (bid,),
        )
        notes_ch = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # файл + якорь → глава, содержащая якорь
    r = client.get(f"/goto/{bid}/Text/notes.xhtml?frag=vv-1")
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/story/{notes_ch}#vv-1")
    # без якоря → первая глава файла
    r = client.get(f"/goto/{bid}/Text/ch1.xhtml")
    assert r.headers["Location"].endswith(f"/story/{ch1}")
    # файл не импортирован → первая глава книги, а не 404
    r = client.get(f"/goto/{bid}/Text/nope.xhtml")
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/story/{ch1}")
    # неизвестная книга → 404
    assert client.get("/goto/99999/Text/x.xhtml").status_code == 404


# ---------------------------------------------------------------- Q8: security + регресс

def test_login_next_rejects_protocol_relative(client):
    """next=//host и /\\host — протокол-относительные в браузере: не уводим наружу."""
    r = client.post("/admin", data={"action": "set_password", "password": "secret"})
    assert r.status_code == 302
    client.get("/logout")
    for bad in ("//evil.example", "/\\evil.example", "/../evil.example"):
        r = client.post(
            f"/login?next={urllib.parse.quote(bad)}",
            data={"password": "secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["Location"]
        assert loc.startswith("/") and not loc.startswith("//")
        assert "evil" not in loc
    # локальный next работает как раньше
    r = client.post(
        "/login?next=/settings", data={"password": "secret"},
        follow_redirects=False,
    )
    assert r.headers["Location"] == "/settings"


def test_file_routes_reject_path_traversal(client):
    """/covers и /media не отдают файлы за пределами своих каталогов."""
    for path in (
        "/covers/..%2F..%2Fapp.db",
        "/covers/....//....//app.db",
        "/media/images/1/..%2F..%2F..%2Fapp.db",
        "/media/images/1/../../../app.db",
    ):
        assert client.get(path).status_code in (400, 404), path


def test_restore_rejects_zipslip(client):
    """Архив с путями ../ не распаковывается и не портит данные."""
    seed()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("app.db", "не база".encode())
        zf.writestr("../evil.txt", b"pwned")
    buf.seek(0)
    r = client.post(
        "/api/backup/restore",
        data={"file": (buf, "evil.zip")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    assert not os.path.exists(os.path.join(db.DATA_DIR, "..", "evil.txt"))
    # библиотека не тронута
    assert "Тест" in client.get("/").get_data(as_text=True)


def test_smoke_all_get_routes(client):
    """Регресс: каждый GET-роут из url_map отвечает без 5xx."""
    import app as app_module

    seed()  # книга id=1, глава id=1
    checked = 0
    for rule in sorted(app_module.app.url_map.iter_rules(), key=lambda r: r.rule):
        if "GET" not in rule.methods or rule.endpoint == "static":
            continue
        path = re.sub(r"<int:\w+>", "1", rule.rule)
        path = path.replace("<path:src>", "x.xhtml")
        r = client.get(path, follow_redirects=False)
        assert r.status_code < 500, f"{rule.rule} -> {r.status_code}"
        checked += 1
    assert checked >= 15, f"обойдено только {checked} роутов"


# ---------------------------------------------------------------- R7: статистика

def test_progress_writes_read_log(client):
    """Прогресс чтения попадает в журнал по дням (слов, максимум за день)."""
    cid = seed()  # глава: 4 слова
    r = client.post("/api/progress", json={"chapter_id": cid, "pct": 50, "done": 0})
    assert r.status_code == 200
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT words FROM read_log WHERE chapter_id = ? AND day = ?",
            (cid, db.today()),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["words"] == 2  # 4 слова × 50%

    # прокрутили назад и послали меньший pct — дневной максимум не падает
    client.post("/api/progress", json={"chapter_id": cid, "pct": 30, "done": 0})
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT words FROM read_log WHERE chapter_id = ? AND day = ?",
            (cid, db.today()),
        ).fetchone()
    finally:
        conn.close()
    assert row["words"] == 2


def test_stats_page(client):
    """/stats: сегодняшние слова, серия, прогресс книги."""
    cid = seed()
    client.post("/api/progress", json={"chapter_id": cid, "pct": 100, "done": 1})
    page = client.get("/stats").get_data(as_text=True)
    assert "Статистика чтения" in page
    assert "слов прочитано сегодня" in page
    assert "дней подряд" in page
    assert "100%" in page and "Тест" in page  # книга прочитана
    assert "Последние 7 дней" in page


def test_streak_series():
    """Серия дней подряд: живёт без сегодняшнего чтения, рвётся на пропуске."""
    import app as app_module

    today = db.today()
    day = lambda n: (date.fromisoformat(today) - timedelta(days=n)).isoformat()
    s = app_module._streak
    assert s([], today) == 0
    assert s([today], today) == 1
    assert s([today, day(1)], today) == 2
    assert s([today, day(1), day(2)], today) == 3
    assert s([day(1)], today) == 1  # сегодня ещё не читали — серия жива
    assert s([day(2)], today) == 0  # вчера пропуск — серии нет
    assert s([today, day(2)], today) == 1  # вчера пропуск — серия с сегодня
