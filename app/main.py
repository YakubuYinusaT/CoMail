"""Cohandle API entry point.

Deliberately thin. Its job right now is to boot on Cloud Run, prove it can
reach Neon, and give the deploy something to health-check. The pipeline
described in the build spec hangs off this.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

DATABASE_URL = os.environ.get("DATABASE_URL", "")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

_engine: Engine | None = None


def _sqlalchemy_url(url: str) -> str:
    """Point SQLAlchemy at psycopg 3.

    Neon hands out `postgresql://...`, which SQLAlchemy resolves to
    psycopg2 — a driver this image does not install. Rewriting the scheme
    here means the connection string can be pasted from the Neon console
    or Secret Manager unchanged, instead of everyone having to remember
    to edit it.
    """
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def engine() -> Engine:
    """One pooled engine per process.

    Cloud Run runs several containers under load and Neon's pooler counts
    every connection, so keep the per-process pool small: the effective
    total is pool_size multiplied by the number of live instances.
    """
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _engine = create_engine(
            _sqlalchemy_url(DATABASE_URL),
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,   # Neon suspends idle computes; revalidate
            pool_recycle=300,
            future=True,
        )
    return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly at boot rather than on the first real request. Cloud Run
    # will not route traffic to a revision whose container never became
    # ready, so a bad DATABASE_URL stops the rollout instead of reaching
    # users.
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set; refusing to start")
    yield
    if _engine is not None:
        _engine.dispose()


app = FastAPI(title="Cohandle", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness. Must not touch the database.

    Cloud Run's startup probe hits this. If it queried Neon, a suspended
    compute waking up would read as a failed deploy.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, object]:
    """Readiness. Does touch the database, on purpose."""
    with engine().connect() as conn:
        version = conn.execute(text("select version()")).scalar_one()
        database = conn.execute(text("select current_database()")).scalar_one()
    return {"status": "ok", "database": database, "server": version}


def require_internal(request: Request) -> None:
    """Guard for endpoints the scheduler calls rather than a person."""
    if not INTERNAL_TOKEN:
        raise HTTPException(503, "INTERNAL_TOKEN is not configured")
    if request.headers.get("x-internal-token") != INTERNAL_TOKEN:
        raise HTTPException(401, "bad or missing internal token")


@app.post("/internal/poll-replies")
def poll_replies(request: Request) -> JSONResponse:
    """Invoked on a schedule by the poller job.

    Scans the Zoho inbox for replies to anything Cohandle sent and halts
    further contact with those targets. Not implemented yet; the route
    exists so the scheduler and its auth can be wired and tested before
    the logic lands.
    """
    require_internal(request)
    return JSONResponse({"status": "not_implemented", "checked": 0})
