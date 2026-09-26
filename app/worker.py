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

from app.db import session
from app.models import Assessment, Contact, Job, Send, Target

# crawl, extract, generate and qualify are imported inside the handlers
# below, not here. Between them they pull in trafilatura, which takes
# seven seconds to import and is not needed until a job actually runs.
# The web app imports this module at startup, and on a laptop that seven
# seconds is the difference between double-clicking and seeing a window.

log = logging.getLogger("cohandle.worker")

ENRICH = "enrich"
GENERATE = "generate"
SEND = "send"
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

# Idle polling backs off rather than hammering the database every few
# seconds forever. It matters more than it looks: a query every three
# seconds means Neon's compute never auto-suspends, and on Cloud Run with
# an always-on instance that is 24 hours a day of compute time bought to
# discover an empty table. Backing off does not cost responsiveness,
# because enqueue() wakes this loop directly.
IDLE_MIN = 2.0
IDLE_MAX = 60.0

_wake: asyncio.Event | None = None
_wake_loop: asyncio.AbstractEventLoop | None = None


def nudge() -> None:
    """Wake the worker in this process now, rather than at the next poll.

    Only reaches the local loop. Another instance picks the job up on its
    own backoff, within a minute at worst, which is why the backoff is
    capped rather than unbounded.
    """
    if _wake is None or _wake_loop is None:
        return
    try:
        _wake_loop.call_soon_threadsafe(_wake.set)
    except RuntimeError:
        # The loop has closed, which happens during shutdown.
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def queued_domains(kind: str) -> set[str]:
    """Domains with a job of this kind already waiting or in flight."""
    with session() as s:
        rows = s.scalars(select(Job).where(Job.kind == kind, Job.state.in_(LIVE)))
        return {j.payload.get("domain") for j in rows if j.payload.get("domain")}


def enqueue(kind: str, **payload) -> bool:
    """Add a job unless this exact one is already waiting or running.

    Deduplicates on the whole payload rather than on the domain alone.
    The sweep that queues due sends runs on every idle pass, and keying
    on domain would have let it add a fresh job for the same send every
    few seconds until the window opened.

    Returns whether anything was added, so a caller can tell "started"
    from "already going" rather than silently queueing the same crawl
    twice.
    """
    with session() as s:
        existing = s.scalar(
            select(func.count()).select_from(Job).where(
                Job.kind == kind, Job.state.in_(LIVE), Job.payload == payload))
        if existing:
            return False
        s.add(Job(kind=kind, payload=payload, state="pending"))
    nudge()
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


def sweep_sends() -> int:
    """Queue a job for every send that is now due.

    Sends wait on a clock rather than on the queue, so something has to
    notice when the window opens. This runs on the idle pass, which is
    exactly when nothing else is happening.
    """
    from app.sending import due

    queued = 0
    for send_id in due():
        if enqueue(SEND, send_id=send_id):
            queued += 1
    return queued


# Checking the Sent folder is a read against somebody else's server, so
# it happens on a slow beat rather than on every idle pass. Ten minutes
# is far quicker than a person would notice a draft going out.
RECONCILE_EVERY = timedelta(minutes=10)
_last_reconcile: datetime | None = None


def sweep_reconcile() -> None:
    """Mark drafts that have since been sent, occasionally.

    Failures are swallowed on purpose. The common one is a token that
    cannot read the mailbox, which is a setup step rather than a fault,
    and it must not fill the log or stop the queue from running.
    """
    global _last_reconcile
    now = _now()
    if _last_reconcile is not None and now - _last_reconcile < RECONCILE_EVERY:
        return
    _last_reconcile = now

    from app import reconcile

    try:
        report = reconcile.run()
    except Exception:  # noqa: BLE001 - never let a read stop the worker
        log.debug("reconcile failed", exc_info=True)
        return
    if report.matched:
        log.info("reconcile: %s", report)

    # Replies ride on the same beat. Both are reads of the same mailbox,
    # and knowing a fund answered matters far more than the ten minutes
    # it might wait.
    from app import replies

    try:
        answered = replies.run()
    except Exception:  # noqa: BLE001 - same reasoning
        log.debug("reply poll failed", exc_info=True)
        return
    if answered.replies or answered.bounced:
        log.info("replies: %s", answered)

    # Follow-ups last, and deliberately after the reply poll: a fund that
    # answered in the last ten minutes must not be nudged in the next
    # ten. Writing one costs a model call, so a failure is logged and
    # dropped rather than retried on the next beat.
    from app import followup

    try:
        nudges = followup.run()
    except Exception:  # noqa: BLE001 - same reasoning
        log.debug("follow-up pass failed", exc_info=True)
        return
    if nudges.written or nudges.failed:
        log.info("follow-ups: %s", nudges)


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
    from app.crawl import crawl_domain
    from app.extract import extract_target

    report = await crawl_domain(domain)
    if report.blocked_by_robots:
        raise RuntimeError("robots.txt forbids crawling this site")
    if report.pages == 0:
        detail = f": {report.errors[0]}" if report.errors else ""
        raise RuntimeError(f"nothing could be fetched{detail}")
    # extract_target is synchronous and spends most of its time waiting on
    # the model, so it goes to a thread rather than stalling the loop.
    await asyncio.to_thread(extract_target, domain)
    await asyncio.to_thread(_score, domain)


def _score(domain: str) -> None:
    """Score and route the target, right after reading it.

    Scoring costs nothing: it is rules over the extracted profile, with no
    model call, which is why it does not belong behind the Draft button
    with generation. Leaving it until then meant `channel` was empty on
    every freshly read target, so a fund that publishes a form still read
    as waiting for an email address.

    Failing here must not fail the enrichment. The pages and the facts are
    the expensive part and they are already stored.
    """
    from app.models import Campaign
    from app.qualify import qualify_campaign

    with session() as s:
        campaign = s.scalar(select(Campaign).order_by(Campaign.created_at.desc()))
        name = campaign.name if campaign else None
    if not name:
        return
    try:
        qualify_campaign(name, only=[domain])
    except Exception as exc:  # noqa: BLE001 - a score is not worth losing a crawl
        log.warning("could not score %s: %s", domain, exc)


def _qualify_then_generate(domain: str, campaign: str) -> None:
    from app.generate import generate_campaign
    from app.qualify import qualify_campaign

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


async def _run_send(send_id: str) -> None:
    """Hand one send to Zoho, or leave it queued if it is not its turn.

    A send outside the window is not a failure and must not burn an
    attempt: the job is finished, the row keeps its own schedule, and the
    sweep picks it up when the window opens.
    """
    from app.sending import dispatch_one

    state = await asyncio.to_thread(dispatch_one, send_id)
    if state == "failed":
        with session() as s:
            row = s.get(Send, send_id)
            raise Permanent(row.error if row and row.error else "send failed")


async def _run_followup(domain: str) -> None:
    """Write a follow-up because a person asked for this one.

    Pressing the button waives the waiting period and nothing else, so a
    refusal here is a real reason and is worth surfacing rather than
    retrying.
    """
    from app import followup

    report = await asyncio.to_thread(
        followup.run, None, domain, True)
    if report.failed:
        raise Permanent(report.failed[0][1])
    if not report.written:
        ok, why = await asyncio.to_thread(followup.status, domain)
        raise Permanent(why or "no follow-up was owed")


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
        elif kind == GENERATE and payload.get("followup"):
            await _run_followup(domain)
        elif kind == GENERATE:
            await _run_generate(domain, payload["campaign"])
        elif kind == SEND:
            await _run_send(payload["send_id"])
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
    global _wake, _wake_loop
    _wake = asyncio.Event()
    _wake_loop = asyncio.get_running_loop()

    reclaim_stale()
    delay = IDLE_MIN
    while not stop.is_set():
        try:
            did_work = await run_one()
        except Exception:  # noqa: BLE001 - a claim failure must not kill the loop
            log.exception("worker loop error")
            did_work = False

        if did_work:
            delay = IDLE_MIN
            continue

        # Only sweep for abandoned jobs once the backoff has stretched
        # out. Doing it on every idle pass was a second query per poll for
        # something that can only become true after fifteen minutes.
        if delay >= IDLE_MAX:
            reclaim_stale()
            sweep_sends()
            sweep_reconcile()

        await _sleep_or_wake(stop, delay)
        delay = min(delay * 2, IDLE_MAX)

    _wake = _wake_loop = None


async def _sleep_or_wake(stop: asyncio.Event, delay: float) -> None:
    """Wait out the backoff, unless something is queued or we are stopping."""
    assert _wake is not None
    waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(_wake.wait())]
    try:
        await asyncio.wait(waiters, timeout=delay,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiters:
            task.cancel()
        _wake.clear()
