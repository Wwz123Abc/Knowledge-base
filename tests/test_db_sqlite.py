from sqlalchemy import text

from app.db import engine


def test_sqlite_uses_wal_and_busy_timeout():
    if engine.dialect.name != "sqlite":
        return

    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 30000
