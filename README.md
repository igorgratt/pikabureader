# PikaBuReader

Локальная читалка для электронных книг в формате ленты Pikabu: книга — «сообщество», глава — «пост», заметки — «комментарии», оценки глав — «плюсы/минусы».

**Версия:** `1.0.0` — см. [CHANGELOG.md](CHANGELOG.md); версии отмечены тегами `vX.Y.Z` в git.

## Возможности

- Импорт **EPUB**, **FB2** (и `fb2.zip`), **PDF** (PyMuPDF), **TXT/Markdown** — через `/add`, drag&drop, несколько файлов; предпросмотр с выбором режима; дубликаты и ошибки — читаемыми карточками
- Фоновая обработка импорта: POST `/add` сразу возвращает статус-страницу с JS-поллингом; preview-книг кэшируется (pickle)
- Картинки в книгах конвертируются в **WebP** (≤800px) при импорте
- Полнотекстовый **поиск по главам** (FTS5) с превью и подсветкой совпадения
- Лента: **Горячее** / **Новое** / **Библиотека** / **Сейчас читаю**; фильтр по тегу, сортировка (новые/название/автор/прогресс)
- Страница книги: обложка, автор, оглавление, прогресс, «Продолжить чтение»
- Страница главы: чтение, навигация `←`/`→` и свайпами, настройки (шрифт, размер, ширина, светлая/тёмная), прогресс-бар, кнопка «Следующая глава»
- **Горячие клавиши**: `←`/`→` — главы, `B` — закладка, `/` — поиск
- Оценки ▲/▼ — у каждого читателя свой голос; закладки с цитатой; автоматический прогресс чтения
- Заметки-комменты: ветвление (ответы), оценка, цитата к выделенному тексту, правка с датой; экспорт в Markdown
- **Все заметки** `/notes`: поиск, фильтр по книге, сортировка, subtabs по типу
- **Свои шрифты**: загрузка TTF/OTF → `/settings/font`; свой шрифт применяется в reader
- **Автопрокрутка**: rAF-петля, 7 скоростей, стоп по wheel/touch/key/visibility
- **Статистика чтения**: график слов/день, серия дней, средняя скорость
- Общий сервер: **пароль инстанса** + **профили читателей** со своим прогрессом/заметками/оценками
- **Передача библиотеки**: экспорт/импорт zip с книгами и обложками
- **Бэкап/восстановление**: one-click zip, восстановление из архива
- **CLI** `pika.py`: serve / import / list / stats / backup (`--json`)
- **Панель владельца** `/admin`: пароль, профили, бэкап, лог импорта, FTS-переиндексация
- **Публичная ссылка** на книгу: «Поделиться» → `/share/<token>`, read-only без авторизации
- **Синхронизация** между устройствами: device tokens + `GET/PUT /api/sync` (last-write-wins)
- PWA: manifest + service worker, офлайн-статика; «Добавить на главный экран» в Chrome/Android
- **Android**: TWA-обёртка — `tools/build_android.ps1` + Bubblewrap → APK
- Понятная на Pikabu вёрстка: карточки постов, шапка, сайдбары, тёмная тема, контраст WCAG AA

## Запуск

```bash
pip install -r requirements.txt
python app.py
```

Открыть http://127.0.0.1:8000

### CLI (без браузера)

```bash
python pika.py serve --port 8000        # то же, что python app.py
python pika.py import book.epub report.fb2 --split compact
python pika.py list --json              # список книг для скриптов
python pika.py stats                    # версия, схема, счётчики, размер data/
python pika.py backup data-backup.zip   # бэкап data/ в zip
```

Коды возврата: 0 — успех, 1 — ошибка, 2 — неверные аргументы.
Подробности — `python pika.py --help`.

### Docker

```bash
docker compose up -d
```

`data/` хранится на хосте как volume. HEALTHCHECK по `/healthz`. CI проверяет сборку и запуск на каждый push.

### Пароль и читатели

- Пароль задаётся в **панели владельца** `http://127.0.0.1:8000/admin` (пока не задан — доступ открыт)
- Профили — на странице аватара в шапке (`/profiles`): свой прогресс, заметки и оценки у каждого

### С телефона / для друзей

См. [docs/deploy.md](docs/deploy.md) — полное руководство по деплою (VPS, Docker + Caddy, systemd).

Кратко:

```bash
HOST=0.0.0.0 python app.py        # Windows: set HOST=0.0.0.0 && python app.py
```

Открой на телефоне `http://<IP-компьютера>:8000`. Из интернета — с HTTPS и паролем.

Полная документация: [docs/](docs/) — [architecture.md](docs/architecture.md), [faq.md](docs/faq.md), [security.md](docs/security.md), [deploy.md](docs/deploy.md), [android.md](docs/android.md), [backlog.md](docs/backlog.md).

## Структура

```
app.py            # Flask-приложение: роуты, API, импорт
db.py             # SQLite-схема (data/app.db, схема v10)
parsers/__init__.py  # парсеры EPUB/FB2/PDF/TXT/MD, санитайзер HTML
templates/         # Jinja-шаблоны
static/css/        # CSS в стиле Pikabu (светлая/тёмная)
static/js/         # reader.js: рейтинги, заметки, прогресс, настройки
static/manifest.webmanifest  # PWA
twa-manifest.json  # Android TWA config для Bubblewrap
tools/
  build_portable.ps1   # Windows zip-дистрибутив
  build_android.ps1    # Android APK (Bubblewrap)
data/             # база, обложки, книги, шрифты, импорт-очередь
```

## Формат данных

| Таблица | Назначение |
|---------|-----------|
| `books` | книга (название, автор, обложка, теги, аннотация) |
| `chapters` | глава (порядок, заголовок, HTML, слова, source) |
| `profiles` | читатели (для общего сервера) |
| `notes` | заметки-комменты (дерево, цитата, edited_at) |
| `state` | закладка, прочитано, прогресс % × профиль |
| `ratings` | голос ±1: главы/заметки × профиль |
| `read_log` | слова/день для R7 |
| `share_tokens` | токены шеринга D7 |
| `sync_tokens` | device-токены для синхронизации M6 |
| `settings` | тема, шрифт, размер, ширина, хеш пароля |

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q    # 119 тестов
```

CI (GitHub Actions) проверяет pytest, pyflakes, node --check на каждый push.

## Лицензия

[GPLv3](LICENSE)
