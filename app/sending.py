"""When a draft is allowed to leave, and the record that it did.

Approval says an email is fit to send. It does not say send it now. Forty
approvals at midnight is how a domain earns a reputation, so between the
two sits a window, an hourly cap and a daily cap, all from settings and
all deliberately low.

The order of operations matters more than anything else here. The send
row is written and committed before the API call, so a crash between the
two leaves a row in `queued` that a person can look at, rather than an
email that went out with nothing recording it. Sending twice is worse
than not sending, because the second one is what the recipient replies to
with "you already wrote to me".

One send per draft, ever. Re-approving an already sent draft does nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app import zoho
from app.config import settings
from app.db import session
from app.models import Campaign, Contact, Draft, Send

log = logging.getLogger("cohandle.sending")


def zone() -> ZoneInfo:
    return ZoneInfo(settings().send_timezone)


def _hhmm(value: str) -> clock:
    hour, _, minute = value.partition(":")
    return clock(int(hour), int(minute or 0))


@dataclass
class Decision:
    """Whether a send may go now, and when it may go if not."""

    ok: bool
    why: str
    not_before: datetime | None = None


def next_opening(now: datetime) -> datetime:
    """The next moment inside the sending window."""
    start = _hhmm(settings().send_window_start)
    end = _hhmm(settings().send_window_end)
    today_start = now.replace(hour=start.hour, minute=start.minute,
                              second=0, microsecond=0)
    today_end = now.replace(hour=end.hour, minute=end.minute,
                            second=0, microsecond=0)
    if now < today_start:
        return today_start
    if now <= today_end:
        return now
    return today_start + timedelta(days=1)


def in_window(now: datetime) -> bool:
    start = _hhmm(settings().send_window_start)
    end = _hhmm(settings().send_window_end)
    return start <= now.time().replace(tzinfo=None) <= end


def counts(now: datetime) -> tuple[int, int]:
    """Sends already made today and in the last hour."""
    with session() as s:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = s.scalar(
            select(func.count()).select_from(Send)
            .where(Send.state == "sent", Send.sent_at >= day_start)) or 0
        hour = s.scalar(
            select(func.count()).select_from(Send)
            .where(Send.state == "sent",
                   Send.sent_at >= now - timedelta(hours=1))) or 0
    return today, hour


def drafting() -> bool:
    """True when approved emails go to Zoho's Drafts rather than out."""
    return settings().delivery_mode.strip().lower() == "draft"


def may_send(now: datetime | None = None) -> Decision:
    """The whole discipline in one answer."""
    cfg = settings()
    now = now or datetime.now(zone())

    # Drafting reaches nobody, so none of the discipline below applies to
    # it. The window and the caps exist to protect a sending reputation;
    # a message sitting in your own Drafts folder has no reputation to
    # damage and no recipient to annoy.
    if drafting():
        return Decision(True, "drafts go to Zoho for you to send")

    if not cfg.send_enabled:
        return Decision(False, "SEND_ENABLED is false, so nothing is armed")

    if not in_window(now):
        return Decision(False,
                        f"outside {cfg.send_window_start}-{cfg.send_window_end} "
                        f"{cfg.send_timezone}",
                        not_before=next_opening(now))

    today, hour = counts(now)
    if today >= cfg.send_daily_cap:
        return Decision(False, f"daily cap reached ({today}/{cfg.send_daily_cap})",
                        not_before=next_opening(now + timedelta(days=1)))
    if hour >= cfg.send_hourly_cap:
        return Decision(False, f"hourly cap reached ({hour}/{cfg.send_hourly_cap})",
                        not_before=now + timedelta(minutes=15))
    return Decision(True, f"{today}/{cfg.send_daily_cap} today, "
                          f"{hour}/{cfg.send_hourly_cap} this hour")


def signature(campaign: Campaign | None) -> str:
    """Appended at send, never stored in the draft body.

    Kept out of the body so the sameness gate compares what was actually
    written rather than a block of identical text on every email.
    """
    if campaign is None:
        return ""
    return (campaign.sender_profile or {}).get("signature", "").strip()


def full_body(draft: Draft, campaign: Campaign | None) -> str:
    sign = signature(campaign)
    return f"{draft.body}\n\n{sign}\n" if sign else draft.body + "\n"


def queue(draft_id: str) -> str | None:
    """Record the intention to send. Idempotent, never a second row."""
    with session() as s:
        draft = s.get(Draft, draft_id)
        if draft is None or draft.status != "approved":
            return None
        existing = s.scalar(select(Send).where(Send.draft_id == draft.id))
        if existing is not None:
            return str(existing.id)
        if draft.contact_id is None:
            log.warning("draft %s approved with no address", draft_id)
            return None
        row = Send(draft_id=draft.id, state="queued")
        s.add(row)
        s.flush()
        return str(row.id)


def dispatch_one(send_id: str, now: datetime | None = None) -> str:
    """Take one queued send to Zoho. Returns the state it ended in.

    Every exit writes the row, so a send that failed says why on the
    screen rather than only in a log nobody opens.
    """
    now = now or datetime.now(zone())
    decision = may_send(now)

    with session() as s:
        row = s.get(Send, send_id)
        if row is None or row.state != "queued":
            return "skipped"
        draft = s.get(Draft, row.draft_id)
        contact = s.get(Contact, draft.contact_id) if draft.contact_id else None
        campaign = s.get(Campaign, draft.campaign_id)

        if draft.status != "approved":
            row.state, row.error = "cancelled", f"the draft is {draft.status}"
            return "cancelled"
        if contact is None or not contact.email:
            row.state, row.error = "failed", "no address on the draft"
            return "failed"
        if not decision.ok:
            # Not an error. It waits, and says what it is waiting for.
            row.scheduled_for = decision.not_before
            row.error = decision.why
            return "waiting"

        to = contact.email
        subject = draft.subject
        body = full_body(draft, campaign)

    # Outside the session on purpose: an HTTP call must not hold a
    # database connection open, and the row above is already committed.
    to_drafts = drafting()
    try:
        data = (zoho.create_draft(to, subject, body) if to_drafts
                else zoho.send(to, subject, body))
    except zoho.NotArmed as exc:
        with session() as s:
            s.get(Send, send_id).error = str(exc)
        return "waiting"
    except Exception as exc:  # noqa: BLE001 - the reason belongs on the row
        with session() as s:
            row = s.get(Send, send_id)
            row.state = "failed"
            row.error = f"{type(exc).__name__}: {exc}"
        return "failed"

    with session() as s:
        row = s.get(Send, send_id)
        # "drafted" is not "sent". Nobody has received anything, and the
        # message may sit in Drafts forever, so sent_at stays empty until
        # something has actually gone.
        row.state = "drafted" if to_drafts else "sent"
        row.sent_at = None if to_drafts else datetime.now(zone())
        row.error = None
        row.zoho_message_id = str(data.get("messageId") or "") or None
        # Zoho does not always name the thread field the same way; keep
        # whichever is present so replies can be threaded later.
        row.thread_id = str(data.get("threadId") or "") or None
    log.info("%s %s to %s", "drafted" if to_drafts else "sent", send_id, to)
    return "drafted" if to_drafts else "sent"


def due(limit: int = 20) -> list[str]:
    """Queued sends whose wait is over."""
    now = datetime.now(zone())
    with session() as s:
        rows = s.scalars(
            select(Send).where(Send.state == "queued")
            .order_by(Send.created_at).limit(limit)).all()
        return [str(r.id) for r in rows
                if r.scheduled_for is None or r.scheduled_for <= now]
