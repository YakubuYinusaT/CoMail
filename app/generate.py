"""Generation.

Not a template with a name swapped in. The opening is chosen by what the
research actually found, which is why the drafts come out structurally
different rather than only lexically different.

Three stages: pick the hook, compose from it, then measure the batch
against itself and regenerate anything that reads as a variant.

The rule that shapes everything: if no verified fact supports any hook,
the target is flagged for manual research and no draft is written. A warm
opening built on nothing is worse than a plain one, and it is exactly
what a model produces when asked to be warm about a fund it knows
nothing about.
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
from app.models import Campaign, Contact, Draft, DraftFact, Fact, Target
from app.qualify import SenderProfile, _parse_when
from app.sameness import compare_batch
from app.taxonomy import canonical_sector, normalise_all

HookType = Literal[
    "portfolio_adjacent", "thesis_quote", "recent_cluster", "publication", "fund_event"
]

# Order is priority: the first hook whose evidence exists wins.
HOOK_ORDER: list[HookType] = [
    "portfolio_adjacent",
    "thesis_quote",
    "recent_cluster",
    "publication",
    "fund_event",
]

FUND_EVENT_RE = re.compile(
    r"\b(clos(e|ed|ing)|raised|launch(ed)?|new fund|fund (i|ii|iii|\d)|"
    r"\$\d+(\.\d+)?\s*(m|million|bn|billion))\b",
    re.I,
)


@dataclass
class Hook:
    kind: HookType
    facts: list[Fact]
    rationale: str


@dataclass
class NoHook:
    reason: str


# --------------------------------------------------------------------------
# stage 1: choose the hook
# --------------------------------------------------------------------------

def _quotable(facts: list[Fact]) -> list[Fact]:
    """Inferred facts may inform scoring. They are never quoted."""
    return [f for f in facts if f.confidence == "verified"]


def choose_hook(
    facts: list[Fact], profile: dict, sender: SenderProfile
) -> Hook | NoHook:
    """First hook with real evidence behind it, in priority order."""
    usable = _quotable(facts)
    by_kind: dict[str, list[Fact]] = {}
    for fact in usable:
        by_kind.setdefault(fact.kind, []).append(fact)

    ours = normalise_all(sender.sectors, canonical_sector)

    for kind in HOOK_ORDER:
        if kind == "portfolio_adjacent":
            # A portfolio company only counts as adjacent when its own
            # description lands in a sector we work in. Otherwise the
            # opening is "you backed a company" — true, and worthless.
            hits = [
                f for f in by_kind.get("portfolio", [])
                if canonical_sector(f.claim) in ours
            ]
            if hits:
                shared = canonical_sector(hits[0].claim)
                return Hook(kind, hits[:2], f"portfolio overlap in {shared}")

        elif kind == "thesis_quote":
            # The quote was checked verbatim against page text during
            # extraction, so if it survived it is safe to put in marks.
            if profile.get("thesis_quote") and by_kind.get("thesis"):
                return Hook(kind, by_kind["thesis"][:1], "verbatim thesis statement")

        elif kind == "recent_cluster":
            recent = [
                f for f in by_kind.get("deal", [])
                if (w := _parse_when(str(f.observed_at.date()))) is not None
                and datetime.now(timezone.utc) - w <= timedelta(days=365)
            ]
            if len(by_kind.get("deal", [])) >= 2:
                return Hook(kind, (recent or by_kind["deal"])[:3],
                            f"{len(by_kind['deal'])} recent deals on record")

        elif kind == "publication":
            if by_kind.get("publication"):
                return Hook(kind, by_kind["publication"][:1], "something they wrote or said")

        elif kind == "fund_event":
            hits = [f for f in by_kind.get("deal", []) if FUND_EVENT_RE.search(f.claim)]
            if hits:
                return Hook(kind, hits[:1], "a fund close or launch")

    if not usable:
        return NoHook("no verified facts; only inferred or none at all")
    kinds = ", ".join(sorted(by_kind)) or "none"
    return NoHook(f"no fact supports an opening (have: {kinds})")


# --------------------------------------------------------------------------
# stage 2: compose
# --------------------------------------------------------------------------

class GeneratedEmail(BaseModel):
    subject: str = Field(max_length=120)
    body: str = Field(max_length=2400)
    used_fact_ids: list[str] = Field(default_factory=list)

    @field_validator("subject", "body")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty")
        return v.strip()


SYSTEM_PROMPT = """\
You write one cold email from a founder to an investor.

You are given facts about the recipient, each with the URL it came from,
and a hook telling you which one to open on. You may state nothing about
the recipient that is not in those facts. If you find yourself wanting to
add context you happen to know, stop: that is the failure this whole
system exists to prevent.

Structure, in this order:

1. Open on what they have done — one or two sentences from the hook
   facts, in your own words. Not flattery. No "I've long admired".
2. The bridge: what the sender is building and why it connects to that
   specific thing.
3. One or two verifiable numbers about the market or the sender.
4. The ask.

Vary these deliberately between emails, because forty of these go out in
the same week and they must not read as one letter with the names
changed:

- length, anywhere from 120 to 220 words
- the middle as prose, or as two or three bullets
- whether the number leads or follows the bridge
- closing on a question or a direct ask
- a subject line drawn from the hook, never a fixed pattern

Never write: an em dash, "I hope this finds you well", "reaching out",
"circling back", "synergy", "excited to", or the recipient's own
marketing adjectives back at them.

Do not write a signature or sign-off. That is appended afterwards.

Return the fact ids you actually used.
"""


def build_prompt(target: Target, hook: Hook, sender: SenderProfile, campaign: Campaign) -> str:
    lines = [
        f"RECIPIENT: {target.name or target.domain} ({target.domain})",
        "",
        f"HOOK: {hook.kind} — {hook.rationale}",
        "Open on the hook facts below, which are marked [HOOK].",
        "",
        "FACTS YOU MAY USE (nothing else about the recipient is permitted):",
    ]
    hook_ids = {str(f.id) for f in hook.facts}
    for fact in _quotable_for_prompt(target, hook):
        mark = "[HOOK] " if str(fact.id) in hook_ids else "       "
        lines.append(f"{mark}id={fact.id} ({fact.kind}) {fact.claim}   <{fact.source_url}>")

    profile = target.profile or {}
    if profile.get("thesis_quote"):
        lines += ["", f'THEIR OWN WORDS (verbatim, safe to quote): "{profile["thesis_quote"]}"']

    lines += [
        "",
        "SENDER:",
        f"  stage: {sender.stage}",
        f"  sectors: {', '.join(sender.sectors)}",
        f"  geography: {sender.geography}",
        f"  raising: ${sender.raise_target_usd:,}" if sender.raise_target_usd else "",
        "",
        "SENDER CONTEXT (true, use what helps):",
        (campaign.sender_profile or {}).get("pitch", "").strip(),
    ]
    return "\n".join(l for l in lines if l is not None)


def _quotable_for_prompt(target: Target, hook: Hook) -> list[Fact]:
    ordered = list(hook.facts)
    for fact in target.facts:
        if fact.confidence == "verified" and fact not in ordered:
            ordered.append(fact)
    return ordered[:12]


def _client():
    cfg = settings()
    if not cfg.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Generation needs it; hook selection "
            "and the sameness gate do not."
        )
    import instructor
    from anthropic import Anthropic

    return instructor.from_anthropic(Anthropic(api_key=cfg.anthropic_api_key))


def compose(
    target: Target, hook: Hook, sender: SenderProfile, campaign: Campaign,
    model: str = "claude-sonnet-5", nudge: str = "",
) -> GeneratedEmail:
    prompt = build_prompt(target, hook, sender, campaign)
    if nudge:
        prompt += f"\n\nADDITIONAL INSTRUCTION: {nudge}"
    return _client().messages.create(
        model=model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        response_model=GeneratedEmail,
    )


# --------------------------------------------------------------------------
# stage 3: the batch gate
# --------------------------------------------------------------------------

@dataclass
class GenerationReport:
    written: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    regenerated: list[str] = field(default_factory=list)
    still_similar: list[str] = field(default_factory=list)
    worst: dict[str, float] = field(default_factory=dict)


def validate_used_facts(email: GeneratedEmail, allowed: list[Fact]) -> list[Fact]:
    """Keep only the facts that exist and were offered.

    Same posture as extraction: an id the model invented is discarded
    rather than chased.
    """
    by_id = {str(f.id): f for f in allowed}
    return [by_id[i] for i in email.used_fact_ids if i in by_id]


def generate_campaign(
    campaign_name: str,
    only: list[str] | None = None,
    model: str = "claude-sonnet-5",
    max_retries: int = 2,
) -> GenerationReport:
    """Draft for every qualified, email-routed target, then gate the batch."""
    report = GenerationReport()
    cfg = settings()

    with session() as s:
        campaign = s.scalar(select(Campaign).where(Campaign.name == campaign_name))
        if campaign is None:
            raise LookupError(f"no campaign named {campaign_name!r}")
        sender = SenderProfile.from_dict(campaign.sender_profile or {})

        stmt = select(Target).where(
            Target.enriched_at.is_not(None), Target.channel == "email"
        )
        if only:
            stmt = stmt.where(Target.domain.in_(only))
        targets = list(s.scalars(stmt))

        drafted: dict[str, tuple[Draft, Hook]] = {}
        for target in targets:
            hook = choose_hook(list(target.facts), target.profile or {}, sender)
            if isinstance(hook, NoHook):
                # The rule that stops a warm opening about nothing.
                report.skipped.append((target.domain, hook.reason))
                continue

            email = compose(target, hook, sender, campaign, model=model)
            used = validate_used_facts(email, _quotable_for_prompt(target, hook))

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
            draft.hook_type = hook.kind
            draft.subject = email.subject
            draft.body = email.body
            draft.status = "generated"
            s.flush()

            for link in list(draft.cited_facts):
                s.delete(link)
            s.flush()
            for fact in used:
                s.add(DraftFact(draft_id=draft.id, fact_id=fact.id))

            drafted[target.domain] = (draft, hook)
            report.written.append(target.domain)

        # The gate. Only meaningful across a batch, which is why it runs
        # here rather than per draft.
        for attempt in range(max_retries + 1):
            bodies = {d: draft.body for d, (draft, _) in drafted.items()}
            batch = compare_batch(bodies, threshold=cfg.sameness_threshold)
            report.worst = batch.worst
            flagged = batch.flagged
            if not flagged or attempt == max_retries:
                report.still_similar = flagged
                break

            for domain in flagged:
                draft, hook = drafted[domain]
                target = s.get(Target, draft.target_id)
                email = compose(
                    target, hook, sender, campaign, model=model,
                    nudge=(
                        "Your previous draft read too much like the others in this "
                        "batch. Change the structure, not just the words: move the "
                        "number, change the length, switch prose and bullets, and "
                        "open on a different sentence."
                    ),
                )
                draft.subject = email.subject
                draft.body = email.body
                if domain not in report.regenerated:
                    report.regenerated.append(domain)

    return report
