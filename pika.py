#!/usr/bin/env python
"""PikaBuReader CLI — сервер, импорт, бэкап и статистика без браузера.

Примеры:
    python pika.py serve --port 8000
    python pika.py import book.epub report.fb2 --split compact
    python pika.py list --json
    python pika.py stats
    python pika.py backup data-backup.zip

Коды возврата: 0 — успех, 1 — ошибка (например, файл не найден),
2 — ошибка аргументов (argparse).
"""
import argparse
import json
import os
import sys


def _bootstrap() -> None:
    """Корень проекта в sys.path — чтобы import app/db работал из любого места."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def cmd_serve(args) -> int:
    _bootstrap()
    import app as app_module

    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app_module.app.run(host=args.host, port=args.port, debug=debug)
    return 0


def cmd_import(args) -> int:
    _bootstrap()
    import app as app_module

    class _DiskFile:
        """Адаптер файлового пути под интерфейс werkzeug FileStorage (.save)."""

        def __init__(self, path):
            self.filename = os.path.basename(path)
            self._path = path

        def save(self, dst):
            import shutil

            shutil.copyfile(self._path, dst)

    results = []
    for path in args.files:
        if os.path.isfile(path):
            r = app_module._import_one(_DiskFile(path), mode=args.split)
        else:
            r = {"file": path, "status": "error", "kind": "Нет файла",
                 "msg": "Файл не найден"}
        r["file"] = r.get("file") or os.path.basename(path)
        results.append(r)
        if not args.json:
            mark = {"ok": "+", "dup": "=", "error": "!"}.get(r["status"], "?")
            print(f"[{mark}] {r['file']}: {r['msg']}")

    ok = all(r["status"] in ("ok", "dup") for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "results": results},
                         ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_list(args) -> int:
    _bootstrap()
    import db

    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT b.id, b.title, b.author,
                      (SELECT COUNT(*) FROM chapters c WHERE c.book_id = b.id) AS chapters,
                      (SELECT COUNT(*) FROM state s JOIN chapters c ON c.id = s.chapter_id
                        WHERE c.book_id = b.id AND s.done = 1) AS done
               FROM books b ORDER BY b.id"""
        ).fetchall()
    finally:
        conn.close()
    books = [dict(r) for r in rows]
    if args.json:
        print(json.dumps({"ok": True, "count": len(books), "books": books},
                         ensure_ascii=False, indent=2))
    else:
        for b in books:
            print(f"{b['id']:>4}  {b['title']} — {b['author']} "
                  f"({b['chapters']} гл., {b['done']} прочит.)")
        print(f"Всего: {len(books)} книг")
    return 0


def cmd_stats(args) -> int:
    _bootstrap()
    import app as app_module
    import db

    conn = db.connect()
    try:
        counts = {
            name: conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
            for name in ("books", "chapters", "notes", "profiles", "state")
        }
    finally:
        conn.close()
    stats = {
        "version": app_module.__version__,
        "schema": db.schema_version(),
        "data_dir": db.DATA_DIR,
        "data_size": app_module._fmt_size(app_module._data_size()),
        **counts,
    }
    if args.json:
        print(json.dumps({"ok": True, "stats": stats}, ensure_ascii=False, indent=2))
    else:
        for key, value in stats.items():
            print(f"{key}: {value}")
    return 0


def cmd_backup(args) -> int:
    from datetime import datetime, timezone

    _bootstrap()
    import app as app_module
    import db

    if not os.path.exists(db.DB_PATH):
        print("База не найдена — сервер ещё не запускался?", file=sys.stderr)
        return 1
    out = args.output or (
        "pikabureader-backup-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.zip"
    )
    with open(out, "wb") as f:
        f.write(app_module._backup_zip_bytes())
    print(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pika.py", description="PikaBuReader CLI (управление без браузера)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="запустить веб-сервер")
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.set_defaults(func=cmd_serve)

    i = sub.add_parser("import", help="импортировать книги (EPUB/FB2/PDF/TXT/MD)")
    i.add_argument("files", nargs="+", metavar="FILE")
    i.add_argument("--split", choices=("parts", "compact", "merge"),
                   default="parts", help="режим разбивки глав (F9)")
    i.add_argument("--json", action="store_true", help="вывод в JSON")
    i.set_defaults(func=cmd_import)

    l = sub.add_parser("list", help="список книг")
    l.add_argument("--json", action="store_true", help="вывод в JSON")
    l.set_defaults(func=cmd_list)

    st = sub.add_parser("stats", help="статистика библиотеки")
    st.add_argument("--json", action="store_true", help="вывод в JSON")
    st.set_defaults(func=cmd_stats)

    b = sub.add_parser("backup", help="бэкап data/ в zip")
    b.add_argument("output", nargs="?", default=None, metavar="ZIP",
                   help="куда записать (по умолчанию — с именем по дате)")
    b.set_defaults(func=cmd_backup)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
