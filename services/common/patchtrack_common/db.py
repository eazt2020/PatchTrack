"""Database access helpers shared by every service."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_pool_max_overflow,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


@contextmanager
def connection() -> Iterator[Connection]:
    """Yield a connection inside a transaction; commits on success."""
    with get_engine().begin() as conn:
        yield conn


def fetch_all(sql: str, **params: Any) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
        return [dict(row) for row in rows]


def fetch_one(sql: str, **params: Any) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(text(sql), params).mappings().first()
        return dict(row) if row else None


def execute(sql: str, **params: Any) -> int:
    with connection() as conn:
        result = conn.execute(text(sql), params)
        return result.rowcount
