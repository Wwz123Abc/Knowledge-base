from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)


if is_sqlite:

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    from app import models  # noqa: F401
    from app.rbac import ensure_default_roles

    # create_all is a dev/sqlite convenience for a zero-config quick start — it only
    # creates tables that don't exist yet and never ALTERs an existing one, so it can't
    # catch or apply schema drift. Production (Postgres) must go through Alembic only
    # (see Dockerfile CMD `alembic upgrade head`); running create_all there too would let
    # a table added to models.py without a migration silently appear outside Alembic's
    # version tracking instead of failing loudly.
    if is_sqlite:
        Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_default_roles(db)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
