"""Q4: E2E smoke (Playwright) — импорт → лента → глава → заметка.

Запускается обычным pytest; без установленного playwright тест скипается.
Браузер — системный Chrome (channel="chrome"), отдельной загрузки не нужно.
"""
import threading

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright не установлен (pip install -r requirements-dev.txt)",
)

from werkzeug.serving import make_server

import db
from test_parsers import make_epub


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Живой HTTP-сервер приложения на эфемерном порту, база во временном каталоге."""
    import app as app_module

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "IMG_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(app_module, "COVER_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(app_module, "IMPORT_LOG", str(tmp_path / "import.log"))
    db.init_db()

    srv = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_e2e_import_feed_story_note(server, tmp_path):
    from playwright.sync_api import sync_playwright

    epub = tmp_path / "smoke.epub"
    make_epub(
        epub,
        [
            ("Начало", "<h2>Начало</h2><p>Первый абзац истории.</p>"),
            ("Продолжение", "<h2>Продолжение</h2><p>Второй абзац истории.</p>"),
        ],
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        try:
            # 1. Импорт: выбрать файл → «Загрузить» → карточка успеха
            page.goto(f"{server}/add")
            page.set_input_files("#file-input", str(epub))
            page.click("#submit-btn")
            page.wait_for_selector(".import-ok", timeout=20000)

            # 2. Лента: карточка главы книги отображается и ведёт на страницу главы
            page.goto(f"{server}/")
            card = page.locator(".story-card").first
            card.wait_for(state="visible", timeout=10000)
            assert page.locator(".story-card").count() >= 1
            card_title = card.locator(".story-title a").inner_text().strip()
            card.locator(".story-title a").click()
            page.wait_for_url("**/story/**", timeout=10000)

            # 3. Страница главы: заголовок совпадает с карточкой, текст на месте
            page.wait_for_selector("#reader", timeout=10000)
            reader = page.locator("#reader")
            if not reader.inner_text().strip():
                page.wait_for_function(
                    "document.querySelector('#reader').innerText.trim().length > 0",
                    timeout=10000,
                )
            assert card_title in page.locator("h1.story-title-lg").inner_text()
            assert "абзац истории" in reader.inner_text()
            assert page.locator("#note-form").count() == 1

            # 4. Заметка: отправить форму → появляется в дереве заметок
            page.fill("#note-text", "E2E-заметка к главе")
            page.click("#note-form button[type=submit]")
            note = page.locator(".notes-tree .note .note-text")
            note.first.wait_for(state="visible", timeout=10000)
            assert "E2E-заметка" in note.first.inner_text()
        finally:
            browser.close()
