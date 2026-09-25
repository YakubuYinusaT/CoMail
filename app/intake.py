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
    """Every target and the stage it has reached.

    A fixed number of queries, not five per target. That mattered less
    when this ran next to the database; running Cohandle on a laptop puts
    every round trip across the internet, and the board polls itself
    while work is in flight.
    """
    with session() as s:
        targets = list(s.scalars(
            select(Target).order_by(Target.created_at.desc()).limit(limit)))
        if not targets:
            return []
        ids = [t.id for t in targets]

        def tally(model, *extra) -> dict:
            stmt = select(model.target_id, func.count()).where(
                model.target_id.in_(ids), *extra).group_by(model.target_id)
            return dict(s.execute(stmt).all())

        pages = tally(Page)
        facts = tally(Fact)
        emails = tally(Contact, Contact.email.is_not(None))

        # Oldest first so the newest wins the key, for all three of these.
        drafts = {d.target_id: d for d in s.scalars(
            select(Draft).where(Draft.target_id.in_(ids))
            .order_by(Draft.created_at))}
        scores = {a.target_id: a.score for a in s.scalars(
            select(Assessment).where(Assessment.target_id.in_(ids))
            .order_by(Assessment.created_at))}
        jobs: dict[tuple[str, str], Job] = {}
        for job in s.scalars(select(Job).where(Job.kind.in_([ENRICH, GENERATE]))
                             .order_by(Job.created_at)):
            domain = job.payload.get("domain")
            if domain:
                jobs[(job.kind, domain)] = job

        rows: list[dict] = []
        for t in targets:
            n_pages = pages.get(t.id, 0)
            n_facts = facts.get(t.id, 0)
            has_email = bool(emails.get(t.id, 0))
            draft = drafts.get(t.id)
            stage, detail = _stage(t, n_pages, n_facts, has_email, draft,
                                   jobs.get((ENRICH, t.domain)),
                                   jobs.get((GENERATE, t.domain)))
            rows.append({
                "domain": t.domain,
                "name": t.name or t.domain,
                "kind": t.kind,
                "stage": stage,
                "label": STAGE_LABEL[stage],
                "detail": detail,
                "pages": n_pages,
                "facts": n_facts,
                "score": scores.get(t.id),
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
