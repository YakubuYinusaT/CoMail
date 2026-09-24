"""The job runner.

`job` has been in the schema since the first migration and until now
nothing wrote to it. Intake is what needs it: pasting twenty funds into a
box cannot be handled inside the request, because crawling and extracting
one target takes the better part of a minute and costs an LLM call.

Two kinds run here.

`enrich` is crawl then extract, and it stops there. It deliberately does
not go on to score and draft, because the whole point of the intake
screen is to see what was found before spending a generation call on it.

`generate` is the explicit continuation, enqueued when you press Draft on
a target you have looked at. It runs qualification first: a draft written
without a score has nothing to decide whether emailing is even the right
move.

Claiming uses `FOR UPDATE SKIP LOCKED`, so two instances running this
loop take different jobs rather than the same one twice.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.crawl import crawl_domain
from app.db import session
from app.extract import extract_target
from app.generate import generate_campaign
from app.models import Assessment, Contact, Job, Target
from app.qualify import qualify_campaign

log = logging.getLogger("cohandle.worker")

ENRICH = "enrich"
GENERATE = "generate"
LIVE = ("pending", "running")


class Permanent(Exception):
    """A job that will never succeed if retried.

    A missing address or a target that did not qualify is a decision, not
    a wobble on the network. Retrying it three times would spend three
    model calls to reach the same answer.
    """

MAX_ATTEMPTS = 3
# A job still marked running after this long belongs to a container that
# died mid-crawl. Cloud Run can take an instance away at any point, so
# this is the ordinary case, not an exceptional one.
STALE_AFTER = timedelta(minutes=15)
IDLE_SLEEP = 3.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def queued_domains(kind: str) -> set[str]:
    """Domains with a job of this kind already waiting or in flight."""
    with session() as s:
        rows = s.scalars(select(Job).where(Job.kind == kind, Job.state.in_(LIVE)))
        return {j.payload.get("domain") for j in rows if j.payload.get("domain")}


def enqueue(kind: str, **payload) -> bool:
    """Add a job unless the same domain is already queued for this kind.

    Returns whether anything was added, so the caller can tell the
    difference between "started" and "already running" rather than
    silently queueing the same crawl twice.
    """
    domain = payload.get("domain")
    if domain:
        with session() as s:
            existing = s.scalar(
                select(func.count()).select_from(Job).where(
                    Job.kind == kind, Job.state.in_(LIVE),
                    Job.payload["domain"].astext == domain))
            if existing:
                return False

    with session() as s:
        s.add(Job(kind=kind, payload=payload, state="pending"))
    return True


def reclaim_stale() -> int:
    """Put abandoned jobs back. Called once at startup and on each idle pass."""
    cutoff = _now() - STALE_AFTER
    returned = 0
    with session() as s:
        for job in s.scalars(select(Job).where(
                Job.state == "running", Job.locked_at < cutoff)):
            job.attempts += 1
            if job.attempts >= MAX_ATTEMPTS:
                job.state = "failed"
                job.error = (job.error or "") + " (abandoned mid-run too many times)"
            else:
                job.state = "pending"
                job.locked_at = None
            returned += 1
    if returned:
        log.info("reclaimed %d stale job(s)", returned)
    return returned


def claim() -> tuple[uuid.UUID, str, dict] | None:
    """Take the oldest ready job. Returns plain values, not a live row.

    The session closes before the work starts, because a crawl can run
    for a minute and the pool only has three connections in it.
    """
    with session() as s:
        job = s.scalar(
            select(Job)
            .where(Job.state == "pending", Job.run_after <= func.now())
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True))
        if job is None:
            return None
        job.state = "running"
        job.locked_at = _now()
        job.attempts += 1
        return job.id, job.kind, dict(job.payload)


def finish(job_id: uuid.UUID, error: str | None = None, *, permanent: bool = False) -> None:
    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        if error is None:
            job.state, job.error = "done", None
            return
        job.error = error[:2000]
        if permanent or job.attempts >= MAX_ATTEMPTS:
            job.state = "failed"
        else:
            # Back off before trying again: a site that timed out once is
            # unlikely to answer immediately.
            job.state = "pending"
            job.locked_at = None
            job.run_after = _now() + timedelta(seconds=30 * job.attempts)


async def _run_enrich(domain: str) -> None:
    report = await crawl_domain(domain)
    if report.blocked_by_robots:
        raise RuntimeError("robots.txt forbids crawling this site")
    if report.pages == 0:
        detail = f": {report.errors[0]}" if report.errors else ""
        raise RuntimeError(f"nothing could be fetched{detail}")
    # extract_target is synchronous and spends most of its time waiting on
    # the model, so it goes to a thread rather than stalling the loop.
    await asyncio.to_thread(extract_target, domain)


def _qualify_then_generate(domain: str, campaign: str) -> None:
    qualify_campaign(campaign, only=[domain])
    report = generate_campaign(campaign, only=[domain])
    if report.written:
        return
    if report.failed:
        raise RuntimeError(report.failed[0][1])

    # Nothing was written and nothing errored, so find out which decision
    # caused it rather than guessing. Generation only drafts for targets
    # that have an address on file, which is the usual answer.
    with session() as s:
        target = s.scalar(select(Target).where(Target.domain == domain))
        if target is None:
            raise Permanent("target no longer exists")
        has_email = s.scalar(
            select(func.count()).select_from(Contact).where(
                Contact.target_id == target.id, Contact.email.is_not(None)))
        if not has_email:
            raise Permanent("no address on file; add one and press Draft again")
        assessment = s.scalar(select(Assessment).where(
            Assessment.target_id == target.id))
        if assessment is not None and assessment.verdict != "qualified":
            raise Permanent(f"assessed as {assessment.verdict}, so no draft was written")
    raise Permanent("nothing was drafted and no reason was given")


async def _run_generate(domain: str, campaign: str) -> None:
    await asyncio.to_thread(_qualify_then_generate, domain, campaign)


async def run_one() -> bool:
    """Claim and run a single job. False means there was nothing to do."""
    taken = claim()
    if taken is None:
        return False
    job_id, kind, payload = taken
    domain = payload.get("domain", "?")
    try:
        if kind == ENRICH:
            await _run_enrich(domain)
        elif kind == GENERATE:
            await _run_generate(domain, payload["campaign"])
        else:
            raise RuntimeError(f"no handler for job kind {kind!r}")
    except Permanent as exc:
        log.info("%s %s stopped: %s", kind, domain, exc)
        finish(job_id, str(exc), permanent=True)
    except Exception as exc:  # noqa: BLE001 - the reason is shown in the UI
        log.warning("%s %s failed: %s", kind, domain, exc)
        finish(job_id, f"{type(exc).__name__}: {exc}")
    else:
        log.info("%s %s done", kind, domain)
        finish(job_id)
    return True


async def loop(stop: asyncio.Event) -> None:
    """Drain the queue until asked to stop."""
    reclaim_stale()
    while not stop.is_set():
        try:
            did_work = await run_one()
        except Exception:  # noqa: BLE001 - a claim failure must not kill the loop
            log.exception("worker loop error")
            did_work = False
        if did_work:
            continue
        reclaim_stale()
        try:
            await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP)
        except asyncio.TimeoutError:
            pass
