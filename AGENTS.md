# AGENTS.md — инструкции для агентов

PikaBuReader — локальная читалка (Flask 3 + SQLite, SSR-шаблоны, без SPA-сборки)
в стиле pikabu.ru. Порт 8000, данные в `data/` (в git не входит).
Русский язык интерфейса и комментариев — часть продукта.

## Команды

| Что | Команда |
|---|---|
| Сервер | `python app.py` (env `HOST`/`PORT`) или `python pika.py serve` |
| Проверка живости | `GET /healthz` → `{"ok": true, "version": "X.Y.Z"}` |
| Тесты | `python -m pytest tests/ -q` (нужен `requirements-dev.txt`; E2E — playwright + системный Chrome, без пакета тест скипается) |
| Линт | `python -m pyflakes app.py db.py parsers pika.py tests` |
| JS-проверка | `node --check static/js/reader.js && node --check static/sw.js` |
| CLI | `python pika.py --help` (serve / import / list / stats / backup, у данных команд `--json`) |

Все четыре проверки (pytest, pyflakes, оба `node --check`) должны быть зелёными
**до** коммита. То же гоняет CI (`.github/workflows/ci.yml`: job `test` + job `docker`).

## Процесс релиза (обязателен для каждой партии изменений)

1. Реализовать батч, прогнать все проверки выше.
2. Обновить `CHANGELOG.md` — секция `## vX.Y.Z — Заголовок (ГГГГ-ММ-ДД)`;
   README — версия и число тестов; `app.py` → `__version__ = "X.Y.Z"`.
3. `git add -A && git commit -m "Bump to vX.Y.Z: ..."`,
   `git tag vX.Y.Z`, `git push origin main --follow-tags`.
4. CI зелёный: `gh run watch <id> --exit-status` (gh = `C:\Program Files\GitHub CLI\gh.exe`).
5. Проверка тега: `git worktree add <tmp> vX.Y.Z` → в worktree
   `python -m pytest tests/ -q` и `python -c "import app; print(app.__version__)"`
   → `git worktree remove <tmp>` (удалять из каталога корня проекта, не из worktree).
6. `docs/backlog.md`: закрытую задачу пометить `*(готово: vX.Y.Z)*` в таблице
   эпика, волну — `✅ (vX.Y.Z)`, поправить «Сводку»/итоги, если часы изменились.

Нумерация версий — по волнам бэклога; каждая партия = коммит + тег.

## Карта проекта

- `docs/backlog.md` — источник правды по задачам: волны в «Порядок работ»,
  DONE в таблицах. Остаток — «Горизонт».
- `docs/architecture.md` — маршруты, JSON API, схема БД, парсеры, тестирование.
- `docs/faq.md` — ответы пользователю (мобильная версия, деплой, доступ, сноски).
- `app.py` — роуты, API, импорт (≈1700 строк), `pika.py` — CLI-обёртка над ними.
- `db.py` — схема/миграции, `parsers/__init__.py` — EPUB/FB2/PDF + санитайзер.

## Подводные камни

- **Тесты** изолируют БД: `monkeypatch.setattr(db, "DATA_DIR"/"DB_PATH", ...)` +
  `db.init_db()`; пути `app_module.UPLOAD_DIR/IMG_DIR/COVER_DIR/IMPORT_LOG` —
  тоже патчить. Готовые фикстуры: `client` (test_app), `cli` (test_cli).
- **Миграции БД**: только `db.SCHEMA_VERSION` + `MIGRATIONS[N]`; catch-up без
  миграции бросает RuntimeError (намеренно). Легаси-данные правятся в миграции.
- **Статика cache-first** в service worker: любая правка `static/` → поднять
  `CACHE` в `static/sw.js` (иначе клиенты не увидят изменений).
- **PowerShell ломает inline-python** с кавычками — писать скрипты во временный
  файл (`C:\Users\user\AppData\Local\Temp\opencode\`) и гонять `python -X utf8 file.py`.
- Веб-импорт использует `_import_one(file, mode)`; CLI подсовывает путь через
  адаптер с `.filename`/`.save(dst)` — не дублировать пайплайн.
- Новый CSS — в `static/css/pikabu.css`; JS — в `static/js/reader.js`
  (чистый, без фреймворков). Селекторы для E2E: `.story-card`, `#reader`,
  `#note-form`, `.import-ok`, `.read-more`.
- Никогда не коммитить `data/`, ключи, `secret_key` (см. `.gitignore`).
