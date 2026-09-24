"""What the intake screen shows.

One row per target, with the stage it has reached. The stage is derived
from what is in the database rather than stored on the target, so a job
that died without writing its status cannot leave a row claiming to be
crawling forever: no pages and no live job reads as stalled, because that
is what it is.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import session
from app.models import Assessment, Contact, Draft, Fact, Job, Page, Target
from app.worker import ENRICH, GENERATE, LIVE

# Order the board by what needs a person, not alphabetically. Failures
# first because they are the only rows that are stuck.
STAGE_ORDER = {
    "failed": 0, "working": 1, "queued": 2, "ready": 3,
    "needs_email": 4, "drafting": 5, "drafted": 6, "stalled": 7,
}

STAGE_LABEL = {
    "queued": "waiting to start",
    "working": "crawling and reading",
    "ready": "read, ready for you",
    "needs_email": "needs an address from you",
    "drafting": "writing the email",
    "drafted": "draft in the queue",
    "failed": "could not finish",
    "stalled": "nothing happened yet",
}


def board(limit: int = 60) -> list[dict]:
    rows: list[dict] = []
    with session() as s:
        jobs: dict[tuple[str, str], Job] = {}
        for job in s.scalars(select(Job).order_by(Job.created_at)):
            domain = job.payload.get("domain")
            if domain:
                jobs[(job.kind, domain)] = job  # last one wins, which is the newest

        for t in s.scalars(select(Target).order_by(Target.created_at.desc()).limit(limit)):
            pages = s.scalar(select(func.count()).select_from(Page)
                             .where(Page.target_id == t.id)) or 0
            facts = s.scalar(select(func.count()).select_from(Fact)
                             .where(Fact.target_id == t.id)) or 0
            draft = s.scalar(select(Draft).where(Draft.target_id == t.id))
            has_email = bool(s.scalar(
                select(func.count()).select_from(Contact).where(
                    Contact.target_id == t.id, Contact.email.is_not(None))))
            assessment = s.scalar(select(Assessment).where(Assessment.target_id == t.id))
            enrich = jobs.get((ENRICH, t.domain))
            generate = jobs.get((GENERATE, t.domain))

            stage, detail = _stage(t, pages, facts, has_email, draft, enrich, generate)
            rows.append({
                "domain": t.domain,
                "name": t.name or t.domain,
                "kind": t.kind,
                "stage": stage,
                "label": STAGE_LABEL[stage],
                "detail": detail,
                "pages": pages,
                "facts": facts,
                "score": assessment.score if assessment else None,
                "draft_id": str(draft.id) if draft else None,
                "has_email": has_email,
                # Only offer Draft once there is something to write from
                # and nothing already in flight.
                "can_draft": stage == "ready",
            })

    rows.sort(key=lambda r: (STAGE_ORDER[r["stage"]], r["name"].lower()))
    return rows


def _stage(target, pages: int, facts: int, has_email: bool,
           draft, enrich, generate) -> tuple[str, str | None]:
    if generate is not None and generate.state in LIVE:
        return "drafting", None
    if draft is not None:
        return "drafted", None
    if generate is not None and generate.state == "failed":
        return "failed", generate.error

    if enrich is not None and enrich.state == "running":
        return "working", f"{pages} pages so far" if pages else "fetching the site"
    if enrich is not None and enrich.state == "pending":
        # Pending after a failure means it is going to try again.
        if enrich.attempts and enrich.error:
            return "queued", f"retrying after: {enrich.error}"
        return "queued", None
    if enrich is not None and enrich.state == "failed":
        return "failed", enrich.error

    if target.enriched_at is not None:
        if not has_email:
            # Cohandle never invents an address, so this one waits on you.
            # The channel note says what the site itself asks for.
            return "needs_email", target.channel_note or None
        if not facts:
            # Worth saying out loud. A draft written from no facts cannot
            # cite anything about them, which is the one thing that makes
            # these emails worth sending.
            return "ready", "nothing quotable was found on the site"
        return "ready", None
    if pages:
        return "stalled", f"{pages} pages fetched but never read"
    return "stalled", None


def counts(rows: list[dict]) -> dict[str, int]:
    out = {"busy": 0, "ready": 0, "failed": 0}
    for r in rows:
        if r["stage"] in ("queued", "working", "drafting"):
            out["busy"] += 1
        elif r["stage"] == "ready":
            out["ready"] += 1
        elif r["stage"] == "failed":
            out["failed"] += 1
    return out
