"""Engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


@lru_cache(maxsize=1)
def engine() -> Engine:
    """One pooled engine per process.

    Cloud Run runs several containers under load and Neon's pooler counts
    every connection, so the per-process pool stays small: the effective
    total is pool_size times the number of live instances.

    `pool_pre_ping` matters more here than on ordinary Postgres. Neon
    suspends an idle compute, and the first query afterwards would
    otherwise hit a dead socket.
    """
    cfg = settings()
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return create_engine(
        cfg.sqlalchemy_url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=300,
        future=True,
    )


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=engine(), expire_on_commit=False, future=True)


@contextmanager
def session() -> Iterator[Session]:
    """Commit on success, roll back on anything else."""
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
