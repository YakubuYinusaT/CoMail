"""Generation.

Ranks the evidence, hands it to the model, and lets the model decide how
to open. The ranking is a suggestion, not a gate.

Two things this deliberately does not do.

It does not refuse to write when there are no verified facts. A fund with
a thin website still deserves an email; it gets one that opens on the
problem instead of on them. What the absence of facts removes is the
right to *claim* anything specific — no portfolio names, no quotes, no
"you backed X". The email still goes.

It does not force an opening. A named portfolio company is one way to
show the research was done, not a tax on every message. Forcing it
produces forty emails that all begin "You backed...", which is the exact
sameness the gate exists to catch.

What stays hard: any statement about the recipient must trace to a
verified fact. That rule is enforced in the prompt and checked against
the fact ids afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.config import settings
from app.db import session
from app.house_style import find as find_slop
from app.house_style import for_prompt as slop_for_prompt
from app.llm import ProviderNotConfigured, complete
from app.models import Campaign, Contact, Draft, DraftFact, Fact, Target
from app.qualify import SenderProfile, _parse_when, email_advice
from app.sameness import compare_batch
from app.taxonomy import canonical_sector, normalise_all

HookKind = Literal[
    "portfolio_adjacent", "thesis_quote", "recent_cluster",
    "publication", "fund_event", "none",
]

FUND_EVENT_RE = re.compile(
    r"\b(clos(e|ed|ing)|raised|launch(ed)?|new fund|fund (i|ii|iii|\d)|"
    r"\$\d+(\.\d+)?\s*(m|million|bn|billion))\b",
    re.I,
)


@dataclass
class Hook:
    kind: HookKind
    facts: list[Fact]
    rationale: str


@dataclass
class Evidence:
    """What is available to open on. Empty is a valid state."""

    hooks: list[Hook] = field(default_factory=list)
    quotable: list[Fact] = field(default_factory=list)
    thesis_quote: str | None = None

    @property
    def best(self) -> Hook | None:
        return self.hooks[0] if self.hooks else None

    @property
    def kind(self) -> HookKind:
        return self.hooks[0].kind if self.hooks else "none"


def gather_evidence(
    facts: list[Fact], profile: dict, sender: SenderProfile
) -> Evidence:
    """Rank every usable opening. Does not choose, and never refuses.

    Inferred facts are excluded from `quotable` because they are not safe
    to state. They still informed qualification, which is where they
    belong.
    """
    quotable = [f for f in facts if f.confidence == "verified"]
    by_kind: dict[str, list[Fact]] = {}
    for fact in quotable:
        by_kind.setdefault(fact.kind, []).append(fact)

    ours = normalise_all(sender.sectors, canonical_sector)
    hooks: list[Hook] = []

    adjacent = [
        f for f in by_kind.get("portfolio", [])
        if canonical_sector(f.claim) in ours
    ]
    if adjacent:
        shared = canonical_sector(adjacent[0].claim)
        hooks.append(Hook("portfolio_adjacent", adjacent[:2],
                          f"portfolio overlap in {shared}"))

    thesis_quote = profile.get("thesis_quote")
    if thesis_quote and by_kind.get("thesis"):
        hooks.append(Hook("thesis_quote", by_kind["thesis"][:1],
                          "verbatim thesis statement"))

    deals = by_kind.get("deal", [])
    if len(deals) >= 2:
        recent = [
            f for f in deals
            if (w := _parse_when(str(f.observed_at.date()))) is not None
            and datetime.now(timezone.utc) - w <= timedelta(days=365)
        ]
        hooks.append(Hook("recent_cluster", (recent or deals)[:3],
                          f"{len(deals)} deals on record"))

    if by_kind.get("publication"):
        hooks.append(Hook("publication", by_kind["publication"][:1],
                          "something they wrote or said"))

    events = [f for f in deals if FUND_EVENT_RE.search(f.claim)]
    if events:
        hooks.append(Hook("fund_event", events[:1], "a fund close or launch"))

    return Evidence(hooks=hooks, quotable=quotable, thesis_quote=thesis_quote)


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------

class GeneratedEmail(BaseModel):
    subject: str = Field(max_length=120)
    body: str = Field(max_length=2400)
    used_fact_ids: list[str] = Field(default_factory=list)
    opened_on: str = Field(default="", max_length=200)

    @field_validator("subject", "body")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty")
        return v.strip()


SYSTEM_PROMPT_TEMPLATE = """\
You write one cold email from a founder to an investor.

Two hard rules, and they cut in both directions.

About the recipient: every statement comes from the supplied facts.
Nothing you happen to know about them, nothing inferred from their name
or their sector. If a fact is not in the list, you do not know it.

About the sender: every claim comes from SENDER CONTEXT below, and that
includes traction. Do not write that they have an MVP, users, revenue,
partners, pilots or signed agreements unless it says so. Inventing the
sender's progress is worse than inventing the recipient's, because the
first meeting is where it falls apart. Where the context states what has
*not* happened yet, respect that. A pre-seed company with no pilot is a
normal thing to be, and saying so plainly reads better than a claim that
will not survive one question.

Carry the sender's hedges across exactly. Upgrading a careful statement
into a confident one is the failure this instruction exists to stop, and
it is not caught by avoiding the banned words. From a real draft:

  context:  Partnership letters sent to EKEDC and Ikeja Electric. No
            signed agreement yet.
  written:  we have initiated partnerships with major players like EKEDC
            and Ikeja Electric
  wrong:    letters sent is not a partnership initiated. The reader will
            ask who signed, and nobody did.
  instead:  we have written to EKEDC and Ikeja Electric

"Approached", "in discussion", "in progress", "working with" and
"engaged" are all the same mistake when what happened was a letter. If
the context does not name a stage, do not invent one.

Everything else is your judgement.

You are given ranked openings. They are suggestions in order of strength,
not instructions. Use one if it genuinely earns the opening. Ignore them
all and open another way if that reads better. A named portfolio company
shows the research was done, but it is not required, and forty emails
that all begin "You backed..." are worse than none of them doing it.

When the facts are thin or absent, that is workable, not a failure. Write
the email without specific claims about them: open on the problem, the
market, or what the sender is building. Do not manufacture familiarity to
fill the gap. An honest general email beats an invented specific one.

Shape:
- open in a way that earns the next sentence
- the bridge: what the sender is building and why it reaches this reader
- a verifiable number or two
- the ask

Vary deliberately across a batch — length between 120 and 220 words,
prose or bullets in the middle, number leading or following, closing on a
question or a direct ask, subject line never a fixed pattern.

Banned outright. These appear in every cold email an investor deletes,
and one of them is enough to make the rest read like the others. Two
kinds: the stock opener vocabulary, and the warm noises that claim a
feeling about the reader. Nobody resonates with a fund's mission
statement, and nothing is unlocked.

{banned}

Their own marketing adjectives quoted back at them are banned too. Where
a word is only a cliche in one sense, the literal sense is fine: supply
that was disrupted is disrupted, and a meter that unlocks unlocks.

Write no greeting line and no sign-off. Not "Dear X", not "Best regards",
not a name. The email starts at the first real sentence and stops at the
last one. Both are added afterwards.

Return the fact ids you used, and one short line in `opened_on` naming
how you chose to open.
"""

# One list, rendered here and checked after generation. Keeping the two
# apart would let them drift, and a prompt rule alone has already been
# shown to be worth less than a check.
SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(banned=slop_for_prompt())


def build_prompt(
    target: Target, evidence: Evidence, sender: SenderProfile, campaign: Campaign
) -> str:
    lines = [f"RECIPIENT: {target.name or target.domain} ({target.domain})", ""]

    if evidence.hooks:
        lines.append("RANKED OPENINGS (suggestions, strongest first):")
        for i, hook in enumerate(evidence.hooks, 1):
            lines.append(f"  {i}. {hook.kind} — {hook.rationale}")
        lines.append("")
    else:
        lines += [
            "NO VERIFIED FACTS ABOUT THIS RECIPIENT.",
            "Write the email anyway, with no specific claims about them. "
            "No portfolio names, no quotes, nothing about what they have "
            "backed. Open on the problem or the market instead.",
            "",
        ]

    if evidence.quotable:
        lines.append("FACTS YOU MAY STATE (nothing else about them is permitted):")
        for fact in evidence.quotable[:14]:
            lines.append(
                f"  id={fact.id} ({fact.kind}) {fact.claim}   <{fact.source_url}>"
            )
        lines.append("")

    if evidence.thesis_quote:
        lines += [f'THEIR OWN WORDS (verbatim, safe to quote): "{evidence.thesis_quote}"', ""]

    profile = campaign.sender_profile or {}
    lines += [
        "SENDER:",
        f"  stage: {sender.stage}",
        f"  sectors: {', '.join(sender.sectors)}",
        f"  geography: {sender.geography}",
    ]
    if sender.raise_target_usd:
        lines.append(f"  raising: ${sender.raise_target_usd:,}")
    if profile.get("pitch"):
        lines += ["", "SENDER CONTEXT (true, use what helps):", profile["pitch"].strip()]

    return "\n".join(lines)


def compose(
    target: Target, evidence: Evidence, sender: SenderProfile, campaign: Campaign,
    model: str | None = None, nudge: str = "",
) -> GeneratedEmail:
    prompt = build_prompt(target, evidence, sender, campaign)
    if nudge:
        prompt += f"\n\nADDITIONAL INSTRUCTION: {nudge}"
    return complete(
        system=SYSTEM_PROMPT,
        user=prompt,
        response_model=GeneratedEmail,
        max_tokens=1500,
        model=model,
    )


def compose_in_house_style(
    target: Target, evidence: Evidence, sender: SenderProfile, campaign: Campaign,
    model: str | None = None, attempts: int = 2,
) -> tuple[GeneratedEmail, list[str]]:
    """Compose, and send it back if it reads like every other cold email.

    The banned list is in the prompt and the model still reaches for these
    words, so it gets told which ones it used and asked again. Naming them
    works far better than repeating the rule.

    Returns whatever the last attempt produced along with anything still
    in it. Nothing is blocked: a draft with one stock phrase in it is a
    draft to edit, not a draft to withhold.
    """
    email = compose(target, evidence, sender, campaign, model=model)
    for _ in range(attempts):
        slop = find_slop(email.subject, email.body)
        if not slop:
            return email, []
        email = compose(
            target, evidence, sender, campaign, model=model,
            nudge=("Your previous draft used " + ", ".join(f'"{s}"' for s in slop)
                   + ". Those are on the banned list. Write it again without "
                     "them, and without reaching for a synonym of the same "
                     "gesture: say the plain thing instead."),
        )
    return email, find_slop(email.subject, email.body)


def validate_used_facts(email: GeneratedEmail, allowed: list[Fact]) -> list[Fact]:
    """Keep only ids that were offered. An invented id is discarded."""
    by_id = {str(f.id): f for f in allowed}
    return [by_id[i] for i in email.used_fact_ids if i in by_id]


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------

@dataclass
class GenerationReport:
    written: list[str] = field(default_factory=list)
    # domain -> (email is recommended, why)
    advice: dict[str, tuple[bool, str]] = field(default_factory=dict)
    without_facts: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    regenerated: list[str] = field(default_factory=list)
    still_similar: list[str] = field(default_factory=list)
    # domain -> stock phrases that survived the rewrites
    house_style: dict[str, list[str]] = field(default_factory=dict)
    worst: dict[str, float] = field(default_factory=dict)


def generate_campaign(
    campaign_name: str,
    only: list[str] | None = None,
    model: str | None = None,
    max_retries: int = 2,
) -> GenerationReport:
    report = GenerationReport()
    cfg = settings()

    with session() as s:
        campaign = s.scalar(select(Campaign).where(Campaign.name == campaign_name))
        if campaign is None:
            raise LookupError(f"no campaign named {campaign_name!r}")
        sender = SenderProfile.from_dict(campaign.sender_profile or {})

        # Anything with an address the operator put there. The channel
        # recommendation rides along with the draft rather than deciding
        # whether one exists.
        stmt = (
            select(Target)
            .where(Target.enriched_at.is_not(None))
            .where(Target.id.in_(
                select(Contact.target_id).where(Contact.email.is_not(None))
            ))
        )
        if only:
            stmt = stmt.where(Target.domain.in_(only))
        targets = list(s.scalars(stmt))

        drafted: dict[str, tuple[Draft, Evidence]] = {}
        for target in targets:
            evidence = gather_evidence(list(target.facts), target.profile or {}, sender)
            if not evidence.hooks:
                # Noted, not skipped. The email is written without
                # specific claims.
                report.without_facts.append(target.domain)

            try:
                email, slop = compose_in_house_style(
                    target, evidence, sender, campaign, model=model)
                if slop:
                    report.house_style[target.domain] = slop
            except ProviderNotConfigured:
                raise
            except Exception as exc:
                report.failed.append((target.domain, f"{type(exc).__name__}: {exc}"))
                continue

            used = validate_used_facts(email, evidence.quotable)

            contact = s.scalar(
                select(Contact).where(
                    Contact.target_id == target.id, Contact.email.is_not(None)
                ).limit(1)
            )
            draft = s.scalar(
                select(Draft).where(
                    Draft.target_id == target.id, Draft.campaign_id == campaign.id
                )
            )
            if draft is None:
                draft = Draft(target_id=target.id, campaign_id=campaign.id)
                s.add(draft)
            draft.contact_id = contact.id if contact else None
            draft.hook_type = evidence.kind
            draft.subject = email.subject
            draft.body = email.body
            draft.status = "generated"
            s.flush()

            for link in list(draft.cited_facts):
                s.delete(link)
            s.flush()
            for fact in used:
                s.add(DraftFact(draft_id=draft.id, fact_id=fact.id))

            drafted[target.domain] = (draft, evidence)
            report.written.append(target.domain)
            report.advice[target.domain] = email_advice(target.profile or {}, True)

        for attempt in range(max_retries + 1):
            if not drafted:
                break
            batch = compare_batch(
                {d: draft.body for d, (draft, _) in drafted.items()},
                threshold=cfg.sameness_threshold,
            )
            report.worst = batch.worst
            flagged = batch.flagged
            if not flagged or attempt == max_retries:
                report.still_similar = flagged
                break

            for domain in flagged:
                draft, evidence = drafted[domain]
                target = s.get(Target, draft.target_id)
                try:
                    email = compose(
                        target, evidence, sender, campaign, model=model,
                        nudge=(
                            "Your previous draft read too much like the others in "
                            "this batch. Change the structure, not just the words: "
                            "move the number, change the length, switch between "
                            "prose and bullets, and open a different way. If you "
                            "opened on a portfolio company, do not this time."
                        ),
                    )
                except Exception as exc:
                    report.failed.append((domain, f"regenerate: {exc}"))
                    continue
                draft.subject = email.subject
                draft.body = email.body
                if domain not in report.regenerated:
                    report.regenerated.append(domain)

    return report
