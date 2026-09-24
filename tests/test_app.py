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
