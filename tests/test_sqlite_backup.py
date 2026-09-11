import sqlite3

from scripts.backup_sqlite import backup_database


def test_sqlite_backup_is_readable_and_complete(tmp_path):
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE sample (value TEXT)")
        database.execute("INSERT INTO sample VALUES ('ready')")

    result = backup_database(source, output)

    with sqlite3.connect(output) as database:
        value = database.execute("SELECT value FROM sample").fetchone()[0]
    assert value == "ready"
    assert result["integrity_check"] == "ok"
