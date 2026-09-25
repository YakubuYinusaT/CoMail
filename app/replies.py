"""Noticing when somebody writes back.

A follow-up sent to a fund that already replied is the worst thing this
tool could do. It undoes the one thing that went right, and it reads as
though nobody was paying attention. So before follow-ups exist at all,
the signal that stops them has to exist and has to be trustworthy.

Matching is on the sender rather than on a thread. Zoho's message headers
carry no thread id, and in any case a reply frequently comes from a
different person at the same fund: you write to hello@fund.com and a
partner answers from their own address. So a message counts as a reply
when the sender is an address we hold for that target, or when the
sender's domain is the target's domain, and it arrived after we wrote.

Three kinds of inbound are told apart, because treating them alike would
be wrong in three different ways:

    a reply, which is a person and stops everything;
    an automatic reply, which is a machine saying someone is away, and
    must not be mistaken for interest;
    a bounce, which means the address is wrong and the send never
    arrived, so the target has not been contacted at all.

Nothing here writes to the mailbox and nothing reads a message body. The
snippet Zoho already puts in the header is enough to recognise what
arrived.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import zoho
from app.db import session
from app.models import Contact, Draft, Reply, Send, Target
from app.reconcile import _addresses, _when

log = logging.getLogger("cohandle.replies")

PAGE = 100
MAX_PAGES = 3

# A message that arrived fractionally before our own send is the send
# itself coming back through a list, not an answer to it.
SLACK = timedelta(minutes=2)

AUTO_REPLY = re.compile(
    r"\b(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|"
    r"away from my (desk|email)|on (annual )?leave|vacation (response|reply)|"
    r"thank you for (your )?(e-?mail|message)[.!]? (i|we) am currently|"
    r"abwesenheitsnotiz|réponse automatique)\b", re.I)

BOUNCE_SUBJECT = re.compile(
    r"\b(undeliverable|delivery status notification|mail delivery (failed|"
    r"subsystem)|returned mail|failure notice|message not delivered|"
    r"delivery has failed)\b", re.I)

BOUNCE_SENDER = re.compile(
    r"^(mailer-daemon|postmaster|no-?reply)@", re.I)


@dataclass
class Report:
    scanned: int = 0
    replies: list[str] = field(default_factory=list)
    auto: list[str] = field(default_factory=list)
    bounced: list[str] = field(default_factory=list)
    note: str = ""

    def __str__(self) -> str:
        if self.note:
            return self.note
        return (f"{self.scanned} inbound scanned, {len(self.replies)} reply, "
                f"{len(self.auto)} automatic, {len(self.bounced)} bounced")


def _inbox_folders() -> list[str]:
    """Every folder inbound mail can land in.

    Three of this account's folders are Inbox-typed, because filters sort
    mail into them, and a reply put into Newsletter by a rule is still a
    reply. Spam is included for the same reason: a missed reply sitting
    in Spam is the expensive kind of miss.
    """
    wanted = {"inbox", "spam"}
    return [str(f.get("folderId")) for f in zoho.folders()
            if str(f.get("folderType", "")).lower() in wanted]


def _headers(folder_id: str) -> list[dict]:
    out: list[dict] = []
    for page in range(MAX_PAGES):
        batch = zoho.messages(folder_id, limit=PAGE, start=page * PAGE + 1)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE:
            break
    return out


def _sender(message: dict) -> str | None:
    found = _addresses(message.get("fromAddress", ""))
    return sorted(found)[0] if found else None


def _text_of(message: dict) -> str:
    return html.unescape(
        f"{message.get('subject', '')} {message.get('summary', '')}")


def _contacted() -> tuple[dict, dict, dict]:
    """Who we have written to, keyed three ways.

    by_address: address we sent to -> (target_id, send_id, sent_at)
    by_domain:  target domain      -> (target_id, send_id, sent_at)
    seen:       (target_id, from, received) already recorded
    """
    by_address: dict[str, tuple] = {}
    by_domain: dict[str, tuple] = {}
    with session() as s:
        for row in s.scalars(select(Send).where(Send.state == "sent")):
            draft = s.get(Draft, row.draft_id)
            if draft is None:
                continue
            contact = s.get(Contact, draft.contact_id) if draft.contact_id else None
            target = s.get(Target, draft.target_id)
            if target is None or contact is None or not contact.email:
                continue
            entry = (target.id, row.id, row.sent_at)
            by_address[contact.email.lower()] = entry
            by_domain.setdefault(target.domain.lower(), entry)

        seen = {(r.target_id, r.from_email, r.received_at)
                for r in s.scalars(select(Reply))}
    return by_address, by_domain, seen


def run() -> Report:
    """Scan inbound mail and record anything answering a send."""
    report = Report()
    by_address, by_domain, seen = _contacted()
    if not by_address:
        report.note = "nothing has been sent yet, so nothing can have replied"
        return report

    try:
        folders = _inbox_folders()
        messages: list[dict] = []
        for folder in folders:
            messages.extend(_headers(folder))
    except zoho.NeedsWiderScope as exc:
        report.note = str(exc)
        return report

    report.scanned = len(messages)

    for message in messages:
        sender = _sender(message)
        received = _when(message)
        if not sender or received is None:
            continue

        text = _text_of(message)

        # A bounce names the address that failed, and comes from a
        # postmaster rather than from the fund, so it is matched on the
        # address quoted inside it.
        if BOUNCE_SENDER.match(sender) or BOUNCE_SUBJECT.search(text):
            for address in _addresses(text):
                entry = by_address.get(address.lower())
                if entry is None:
                    continue
                _mark_bounced(entry[1], sender)
                report.bounced.append(address.lower())
                break
            continue

        domain = sender.partition("@")[2].lower()
        entry = by_address.get(sender) or by_domain.get(domain)
        if entry is None:
            continue

        target_id, send_id, sent_at = entry
        if sent_at and received < sent_at - SLACK:
            continue
        if (target_id, sender, received) in seen:
            continue

        auto = bool(AUTO_REPLY.search(text))
        with session() as s:
            s.add(Reply(
                send_id=send_id, target_id=target_id, received_at=received,
                from_email=sender, is_auto_reply=auto,
                snippet=(html.unescape(message.get("summary", "")) or None),
            ))
        seen.add((target_id, sender, received))
        (report.auto if auto else report.replies).append(sender)
        log.info("%s from %s", "automatic reply" if auto else "reply", sender)

    return report


def _mark_bounced(send_id, sender: str) -> None:
    with session() as s:
        row = s.get(Send, send_id)
        if row is None or row.state == "bounced":
            return
        row.state = "bounced"
        row.error = f"bounced, reported by {sender}"


def answered() -> set:
    """Target ids that have replied, excluding automatic replies.

    An out of office is not an answer, and treating it as one would stop
    a follow-up that should still go.
    """
    with session() as s:
        return {r.target_id for r in s.scalars(
            select(Reply).where(Reply.is_auto_reply.is_(False)))}
