"""Connexion à la base : SQLite par défaut, PostgreSQL/Supabase via ``DATABASE_URL``."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Accepte les URL Supabase ``postgres://`` / ``postgresql://`` et force le driver psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class Database:
    def __init__(self, url: str) -> None:
        self.url = normalize_url(url)
        self.engine = self._create_engine(self.url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _create_engine(url: str) -> Engine:
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite":
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(url, connect_args={"check_same_thread": False})
            event.listen(engine, "connect", _sqlite_pragmas)
            return engine
        return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        logger.info("Schéma de base de données prêt (%s)", self.backend)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
