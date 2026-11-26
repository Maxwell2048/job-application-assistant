"""Engine and session factory. SQLite needs check_same_thread off because the
worker thread and request handlers share the engine."""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(database_url: str):
    if database_url.startswith("sqlite"):
        path = database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(database_url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")  # worker and API threads share the file
        return engine
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine):
    """Create missing tables, then missing indexes on tables that already existed:
    create_all() adds an index only together with its table, so a ledger created
    by an earlier version would never get indexes added to the models later."""
    Base.metadata.create_all(engine)
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(engine, checkfirst=True)
