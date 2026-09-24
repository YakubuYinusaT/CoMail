"""Cohandle API entry point.

Deliberately thin. Its job right now is to boot on Cloud Run, prove it can
reach Neon, and give the deploy something to health-check. The pipeline
described in the build spec hangs off this.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import auth
from app.config import settings
from app.db import engine

log = logging.getLogger("cohandle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly at boot rather than on the first real request. Cloud Run
    # will not route traffic to a revision whose container never became
    # ready, so a bad DATABASE_URL stops the rollout instead of reaching
    # users.
    if not settings().database_url:
        raise RuntimeError("DATABASE_URL is not set; refusing to start")
    # Say which door is open. An unauthenticated deploy should be a
    # decision someone made, not something nobody noticed in the logs.
    if auth.enabled():
        log.info("login is on: shared password, %d day sessions",
                 settings().session_max_age_days)
    else:
        log.warning("login is OFF (APP_PASSWORD is unset). Correct behind "
                    "Identity-Aware Proxy, wrong on a public URL.")
    yield
    engine().dispose()


app = FastAPI(title="Cohandle", lifespan=lifespan)

# Registered before the routes below so it wraps every one of them,
# including /static and the review queue.
app.middleware("http")(auth.gate)

# The review queue. Mounted last so /healthz and /readyz keep their
# meaning for Cloud Run's probes.
from app.web import router as web_router  # noqa: E402

app.include_router(web_router)

from pathlib import Path as _Path  # noqa: E402

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static",
          StaticFiles(directory=_Path(__file__).parent / "static"),
          name="static")


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
    INTERNAL_TOKEN = settings().internal_token
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
