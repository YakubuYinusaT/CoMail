"""Writing the second email.

Most of this file is about when not to write one. Generating the text is
the easy half; deciding that a fund is owed a nudge, and that nudging
them is not a mistake, is where the damage would be.

A follow-up is owed when a first email was actually sent, enough days
have passed, and nobody answered. Every one of those three is checked
against something real rather than assumed. "Actually sent" means the
Sent folder confirmed it, not that a draft was made. "Nobody answered"
means no reply row that is not an out of office, because a machine
saying somebody is away is not an answer and should not stop a nudge
that is still owed.

It stops for a bounce, because nothing arrived and a second copy will not
arrive either, and for a rejected draft, because deciding not to write is
a decision about the fund and not about one email.

What comes out is a draft like any other. It goes into the review queue,
through the same claims and house-style checks, and out to Zoho Drafts
only when approved. Nothing here sends anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db import session
from app.models import Campaign, Contact, Draft, Reply, Send, Target

log = logging.getLogger("cohandle.followup")


@dataclass
class Candidate:
    target_id: object
    domain: str
    campaign_id: object
    contact_id: object
    step: int
    first_subject: str
    first_body: str
    sent_at: datetime


@dataclass
class Report:
    due: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""

    def __str__(self) -> str:
        if self.note:
            return self.note
        return (f"{len(self.due)} due, {len(self.written)} drafted, "
                f"{len(self.failed)} failed")


def verdict(*, send_state: str, sent_at: datetime | None, draft_status: str,
            step: int, answered: bool, already_written: bool,
            now: datetime, after_days: int, max_step: int,
            manual: bool = False) -> str | None:
    """Why this target is not owed a follow-up, or None if it is.

    Every reason is named rather than returned as a bare False, because
    "why did Cohandle not nudge them" is a question somebody will ask,
    and because the rules are the whole point of this module.
    """
    if send_state != "sent":
        # drafted means it may never have gone. bounced means it did not
        # arrive, and a second copy will not arrive either.
        return f"the first email is {send_state}, not sent"
    if sent_at is None:
        return "no send date, so the wait cannot be measured"
    if draft_status == "rejected":
        return "the first draft was rejected, which was a decision about them"
    if answered:
        return "they replied"
    waited = now - sent_at
    if not manual and waited < timedelta(days=after_days):
        # The only rule a person may overrule by pressing the button.
        # Waiting is a courtesy and they can judge it; the others are
        # about whether a second email is defensible at all.
        remaining = timedelta(days=after_days) - waited
        return f"only {waited.days}d since sending, {remaining.days + 1}d to go"
    if step + 1 > max_step:
        return f"already at step {step}, which is the limit"
    if already_written:
        return "a follow-up has already been written"
    return None


def due(now: datetime | None = None, only: str | None = None,
        manual: bool = False) -> list[Candidate]:
    """Every target owed a follow-up, and nothing that is not.

    `manual` means a person asked for this one, which waives the waiting
    period and nothing else. Whether they replied, whether the first
    email ever went, and the step limit are not theirs to waive by
    accident: those are the rules that stop a second email being a
    mistake rather than merely early.
    """
    cfg = settings()
    now = now or datetime.now(timezone.utc)
    candidates: list[Candidate] = []

    with session() as s:
        answered = {r.target_id for r in s.scalars(
            select(Reply).where(Reply.is_auto_reply.is_(False)))}

        for row in s.scalars(select(Send)):
            draft = s.get(Draft, row.draft_id)
            if draft is None:
                continue
            if only is not None:
                target_row = s.get(Target, draft.target_id)
                if target_row is None or target_row.domain != only:
                    continue
            next_step = draft.step + 1
            already = s.scalar(select(Draft).where(
                Draft.target_id == draft.target_id,
                Draft.campaign_id == draft.campaign_id,
                Draft.step == next_step)) is not None

            why = verdict(
                send_state=row.state, sent_at=row.sent_at,
                draft_status=draft.status, step=draft.step,
                answered=draft.target_id in answered, already_written=already,
                now=now, after_days=cfg.followup_after_days,
                max_step=cfg.followup_max_step, manual=manual)
            if why is not None:
                continue

            contact = s.get(Contact, draft.contact_id) if draft.contact_id else None
            target = s.get(Target, draft.target_id)
            if contact is None or target is None:
                continue

            candidates.append(Candidate(
                target_id=draft.target_id, domain=target.domain,
                campaign_id=draft.campaign_id, contact_id=draft.contact_id,
                step=next_step, first_subject=draft.subject,
                first_body=draft.body, sent_at=row.sent_at))
    return candidates


SYSTEM_PROMPT_TEMPLATE = """\
You write one short follow-up email from a founder to an investor who did
not reply to the first one.

You are given the email that was already sent. Do not restate it. The
reader either has it or has deleted it, and repeating the pitch at them
tells them nothing new and wastes the one bit of attention a second email
buys.

Two hard rules carry over. Nothing about the recipient that was not in
the first email, because no new research has been done since. Nothing
about the sender that was not in the first email either, in particular
no new traction, no new partners and no new numbers.

Length: 40 to 90 words. Shorter than the first one, always.

What works: a single sentence naming what you wrote about, one sentence
that gives them an easy way to say no or to say later, and nothing else.
An explicit "if this is not for you, say so and I will stop" is better
than another paragraph of persuasion, because it is honest and it ends
the sequence cleanly either way.

Name the subject in the words the first email used. Paraphrasing it
invents a new description under time pressure, and the direction is the
easiest thing to get backwards: a founder raising money is asking for
investment, not offering any. Never describe the sender as offering
funding, an opportunity or a deal to the reader.

Write it as a person types an email, not as a company writes a notice.
"Say the word and I will stop" rather than "we will cease further
communication".

Banned. The follow-up ones first, because they are what a second email
reaches for: "checking in", "following up", "reaching out", "circling
back", "touching base", "bumping this", "in case you missed it",
"gentle reminder", "per my last email", in any form and any tense. They
all say the same thing, which is that the sender is uncomfortable, and
none of them give the reader a reason to answer. Say what the email was
about instead.

Also banned, the same list every Cohandle email is held to:

{banned}

Do not apologise for writing again. Do not say you know they are busy.

Write no greeting and no sign-off, the same as the first email.

Return the subject and body only. Reuse the first subject prefixed with
"Re: " unless a better one exists.
"""

# The follow-up prompt never carried the main banned list, so the model
# was told about "just following up" and nothing else, and reached
# straight for "reaching out" instead. One list, rendered in both places.
from app.house_style import for_prompt as _slop_for_prompt  # noqa: E402

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(banned=_slop_for_prompt())


def write(candidate: Candidate, model: str | None = None) -> Draft:
    """Generate one follow-up and store it as a draft awaiting review."""
    from app.generate import GeneratedEmail
    from app.house_style import find as find_slop
    from app.house_style import strip_edges
    from app.llm import complete

    days = max(1, (datetime.now(timezone.utc) - candidate.sent_at).days)
    prompt = (
        f"You wrote to {candidate.domain} {days} days ago and they have not "
        f"replied.\n\nTHE EMAIL THAT WAS SENT\n"
        f"Subject: {candidate.first_subject}\n\n{candidate.first_body}\n"
    )

    email = complete(system=SYSTEM_PROMPT, user=prompt,
                     response_model=GeneratedEmail, max_tokens=600, model=model)

    # Two rewrites, the same as a first email gets. Naming the exact
    # words works where repeating the rule does not, which is why find()
    # reports what was written rather than a canonical label.
    for _ in range(2):
        slop = find_slop(email.subject, email.body)
        if not slop:
            break
        email = complete(
            system=SYSTEM_PROMPT,
            user=prompt + "\n\nYour previous attempt used "
                 + ", ".join(f'"{s}"' for s in slop)
                 + ". Those are banned, including any other form of the "
                   "same phrase. Say the plain thing instead.",
            response_model=GeneratedEmail, max_tokens=600, model=model)

    with session() as s:
        draft = Draft(
            target_id=candidate.target_id, campaign_id=candidate.campaign_id,
            contact_id=candidate.contact_id, step=candidate.step,
            hook_type="none", subject=email.subject.strip(),
            body=strip_edges(email.body), status="generated")
        s.add(draft)
        s.flush()
        s.expunge(draft)
    log.info("follow-up %d drafted for %s", candidate.step, candidate.domain)
    return draft


def status(domain: str, now: datetime | None = None) -> tuple[bool, str]:
    """Whether a follow-up can be asked for, and why not if it cannot.

    Used by the button, so the screen can say "they replied" rather than
    showing a control that would do nothing.
    """
    now = now or datetime.now(timezone.utc)
    cfg = settings()
    with session() as s:
        target = s.scalar(select(Target).where(Target.domain == domain))
        if target is None:
            return False, "no such target"

        answered = {r.target_id for r in s.scalars(
            select(Reply).where(Reply.is_auto_reply.is_(False)))}

        reasons: list[str] = []
        for draft in s.scalars(select(Draft).where(
                Draft.target_id == target.id).order_by(Draft.step.desc())):
            row = s.scalar(select(Send).where(Send.draft_id == draft.id))
            if row is None:
                reasons.append("the first email has not been handed to Zoho yet")
                continue
            already = s.scalar(select(Draft).where(
                Draft.target_id == target.id,
                Draft.campaign_id == draft.campaign_id,
                Draft.step == draft.step + 1)) is not None
            why = verdict(
                send_state=row.state, sent_at=row.sent_at,
                draft_status=draft.status, step=draft.step,
                answered=target.id in answered, already_written=already,
                now=now, after_days=cfg.followup_after_days,
                max_step=cfg.followup_max_step, manual=True)
            if why is None:
                return True, ""
            reasons.append(why)
        return False, reasons[0] if reasons else "nothing has been sent yet"


def run(model: str | None = None, only: str | None = None,
        manual: bool = False) -> Report:
    """Draft a follow-up for everything that is owed one."""
    report = Report()
    # A person pressing the button has asked for this one, so the global
    # switch is about the automatic pass rather than about them.
    if not manual and not settings().followups_enabled:
        report.note = "FOLLOWUPS_ENABLED is false"
        return report

    candidates = due(only=only, manual=manual)
    report.due = [c.domain for c in candidates]
    for candidate in candidates:
        try:
            write(candidate, model=model)
        except Exception as exc:  # noqa: BLE001 - one bad target is not all of them
            report.failed.append((candidate.domain, f"{type(exc).__name__}: {exc}"))
            continue
        report.written.append(candidate.domain)
    return report
