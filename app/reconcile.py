"""Finding out which drafts you actually sent.

Cohandle writes an email into Zoho's Drafts folder and records `drafted`.
What happens next is entirely yours: you might send it, edit it heavily
first, sit on it for a week, or delete it. Until now Cohandle had no idea
which, so its record of who you had approached was half a record, and
anything built on top of it, follow-ups most of all, would have been
built on a guess.

This looks in the Sent folder and marks the ones that went.

Matching is on recipient and time, not on the message id. Zoho hands back
an id when the draft is created, and a draft that is edited and sent from
the web client does not reliably keep it. The recipient is the stable
fact: one target, one address, one approach. A subject line is compared
loosely when there is one, because editing the subject before sending is
normal and should not break the match.

Nothing here writes to the mailbox. It reads headers and updates rows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import zoho
from app.db import session
from app.models import Contact, Draft, Send

log = logging.getLogger("cohandle.reconcile")

# How far back to look. A draft older than this that is still unsent is
# not going to be resolved by another sweep.
LOOK_BACK = timedelta(days=90)

# How many Sent headers to pull. Cohandle sends tens of emails, not
# thousands, and this runs on an idle pass rather than in a request.
PAGE = 100
MAX_PAGES = 5


@dataclass
class Report:
    checked: int = 0
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    note: str = ""

    def __str__(self) -> str:
        if self.note:
            return self.note
        return (f"{self.checked} drafted, {len(self.matched)} now marked sent, "
                f"{len(self.unmatched)} still waiting in Drafts")


def _normalise(subject: str) -> str:
    """Enough of a subject to compare after somebody has edited it."""
    text = (subject or "").lower().strip()
    text = re.sub(r"^(re|fwd|fw)\s*:\s*", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _addresses(header: str) -> set[str]:
    """Every address in a To header, however Zoho formatted it."""
    return {a.lower() for a in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", header or "")}


def _when(message: dict) -> datetime | None:
    """Zoho reports times as epoch milliseconds, as a string."""
    for key in ("sentDateInGMT", "receivedTime", "sentDate"):
        raw = message.get(key)
        if raw in (None, ""):
            continue
        try:
            value = int(str(raw))
        except ValueError:
            continue
        # Milliseconds if it is long enough to be, seconds otherwise.
        if value > 10_000_000_000:
            value //= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _sent_headers() -> list[dict]:
    folder = zoho.sent_folder_id()
    if folder is None:
        raise zoho.ZohoError("no Sent folder on this account")

    headers: list[dict] = []
    for page in range(MAX_PAGES):
        batch = zoho.messages(folder, limit=PAGE, start=page * PAGE + 1)
        if not batch:
            break
        headers.extend(batch)
        if len(batch) < PAGE:
            break
    return headers


def run() -> Report:
    """Mark every drafted send that now appears in the Sent folder."""
    report = Report()
    cutoff = datetime.now(timezone.utc) - LOOK_BACK

    with session() as s:
        rows = s.scalars(select(Send).where(Send.state == "drafted")).all()
        pending = []
        for row in rows:
            if row.created_at and row.created_at < cutoff:
                continue
            draft = s.get(Draft, row.draft_id)
            contact = s.get(Contact, draft.contact_id) if draft and draft.contact_id else None
            if draft is None or contact is None or not contact.email:
                continue
            pending.append({
                "send_id": str(row.id),
                "email": contact.email.lower(),
                "subject": _normalise(draft.subject),
                "created_at": row.created_at,
            })

    report.checked = len(pending)
    if not pending:
        report.note = "nothing is waiting in Drafts"
        return report

    try:
        headers = _sent_headers()
    except zoho.NeedsWiderScope as exc:
        report.note = str(exc)
        return report

    for item in pending:
        match = None
        for message in headers:
            if item["email"] not in _addresses(message.get("toAddress", "")):
                continue
            when = _when(message)
            # A message sent before the draft existed is a different one,
            # most likely an earlier approach to the same fund.
            if when and item["created_at"] and when < item["created_at"] - timedelta(minutes=5):
                continue
            subject = _normalise(message.get("subject", ""))
            if item["subject"] and subject and not (
                    subject.startswith(item["subject"][:40])
                    or item["subject"].startswith(subject[:40])):
                continue
            match = (message, when)
            break

        if match is None:
            report.unmatched.append(item["email"])
            continue

        message, when = match
        with session() as s:
            row = s.get(Send, item["send_id"])
            if row is None or row.state != "drafted":
                continue
            row.state = "sent"
            row.sent_at = when or datetime.now(timezone.utc)
            row.zoho_message_id = str(message.get("messageId") or "") or row.zoho_message_id
            row.thread_id = str(message.get("threadId") or "") or row.thread_id
        report.matched.append(item["email"])
        log.info("%s was sent on %s", item["email"], when)

    return report
