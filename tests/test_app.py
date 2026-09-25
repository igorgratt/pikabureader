import io
import zipfile

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
