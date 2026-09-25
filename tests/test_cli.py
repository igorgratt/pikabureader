"""D11: CLI pika.py — список, статистика, импорт, бэкап."""
import json
import zipfile

import pytest

import db
import pika


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Временная БД и каталоги data/ — как в client, но без HTTP."""
    import app as app_module

    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    monkeypatch.setattr(db, "DB_PATH", str(data / "app.db"))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(data / "uploads"))
    monkeypatch.setattr(app_module, "IMG_DIR", str(data / "images"))
    monkeypatch.setattr(app_module, "COVER_DIR", str(data / "covers"))
    monkeypatch.setattr(app_module, "IMPORT_LOG", str(data / "import.log"))
    db.init_db()
    return tmp_path


def test_cli_list_json(cli, capsys):
    from test_app import seed

    seed()
    rc = pika.main(["list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"]
    assert out["count"] == 1
    assert out["books"][0]["title"] == "Тест"
    assert out["books"][0]["chapters"] == 1


def test_cli_list_text(cli, capsys):
    rc = pika.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0 and "Всего: 0 книг" in out


def test_cli_stats_json(cli, capsys):
    from test_app import seed

    seed()
    rc = pika.main(["stats", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    stats = out["stats"]
    assert stats["books"] == 1 and stats["chapters"] == 1
    assert "version" in stats and "schema" in stats


def test_cli_import_and_dup(cli, capsys):
    from test_parsers import make_epub

    epub = cli / "cli.epub"
    make_epub(epub, [("Глава", "<p>Привет из CLI</p>")])

    rc = pika.main(["import", str(epub), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"]
    assert out["results"][0]["status"] == "ok"

    # повторный импорт — дубликат, но код возврата 0 (это не ошибка)
    rc = pika.main(["import", str(epub), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["results"][0]["status"] == "dup"

    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"] == 1
        row = conn.execute("SELECT source FROM chapters LIMIT 1").fetchone()
        assert row["source"]  # колонка source (v0.6.0) заполнена
    finally:
        conn.close()


def test_cli_import_missing_file(cli, capsys):
    rc = pika.main(["import", str(cli / "нет.epub"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and not out["ok"]
    assert out["results"][0]["status"] == "error"


def test_cli_backup(cli, capsys):
    from test_app import seed

    seed()
    out_zip = cli / "bk.zip"
    rc = pika.main(["backup", str(out_zip)])
    assert rc == 0 and out_zip.exists()
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
    assert "app.db" in names


def test_cli_backup_without_db(cli, capsys, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(cli / "нет" / "app.db"))
    rc = pika.main(["backup", str(cli / "x.zip")])
    assert rc == 1


def test_cli_unknown_command():
    with pytest.raises(SystemExit) as exc:
        pika.main(["нет-такой"])
    assert exc.value.code == 2
