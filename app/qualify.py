"""Qualification.

The gate that makes this outreach rather than spam. Scores an extracted
profile against a sender profile, and routes each target to email, a
form, or a warm introduction.

Two things it does that a scoring function usually does not:

Hard gates. A fund that does not invest at your stage, or in your
country, is rejected outright no matter how well it scores elsewhere. No
email talks a Series A fund into a pre-seed cheque, and a high total
built on sector overlap would only hide that.

Routing overrides scoring. A target that publishes an application form,
or says it does not take unsolicited approaches, does not get an email
however well it scores. Cold-mailing a fund whose site says "apply here"
is a wasted shot and reads as not having looked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.db import session
from app.models import Assessment, Campaign, Contact, Target
from app.taxonomy import (
    any_stage_umbrella,
    canonical_sector,
    geo_covers,
    normalise_all,
    stage_floor_above,
    stage_matches,
)

WEIGHTS = {
    "stage": 30,
    "sector": 25,
    "geography": 20,
    "cheque": 10,
    "activity": 10,
    "conflict": 5,
}
QUALIFY_AT = 70
REVIEW_AT = 40
CONFLICT_PENALTY = -10


@dataclass
class SenderProfile:
    """Who is reaching out. Held per campaign."""

    stage: str = "pre-seed"
    raise_target_usd: int | None = None
    sectors: list[str] = field(default_factory=list)
    geography: str = "nigeria"
    competitors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SenderProfile":
        return cls(
            stage=data.get("stage", "pre-seed"),
            raise_target_usd=data.get("raise_target_usd"),
            sectors=list(data.get("sectors", [])),
            geography=data.get("geography", "nigeria"),
            competitors=list(data.get("competitors", [])),
        )


@dataclass
class Criterion:
    name: str
    points: int
    max_points: int
    note: str

    @property
    def zeroed(self) -> bool:
        return self.points <= 0


@dataclass
class Verdict:
    score: int
    verdict: str                     # qualified | needs_review | rejected
    criteria: list[Criterion]
    rejection_reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "criteria": [
                {"name": c.name, "points": c.points, "max": c.max_points, "note": c.note}
                for c in self.criteria
            ],
            "rejection_reason": self.rejection_reason,
        }


def _parse_when(value: str | None) -> datetime | None:
    """Accept anything from '2026-03-14' to 'March 2026' to '2026'."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.search(r"(20\d{2})", text)
    if match:
        return datetime(int(match.group(1)), 6, 1, tzinfo=timezone.utc)
    return None


def score_stage(profile: dict, sender: SenderProfile) -> Criterion:
    stages = profile.get("stages") or []
    cap = WEIGHTS["stage"]
    if not stages:
        # Silence is not a no. Half credit, and the note says why.
        return Criterion("stage", cap // 2, cap, "site does not state a stage")
    if stage_matches(stages, sender.stage):
        return Criterion("stage", cap, cap, f"invests at {sender.stage}")
    if stage_floor_above(stages, sender.stage):
        return Criterion("stage", 0, cap, f"earliest stage is later than {sender.stage}")
    if any_stage_umbrella(stages):
        # "Early stage" spans pre-seed to Series A. Evidence of fit, not
        # confirmation, so it scores well short of a stated match.
        return Criterion("stage", round(cap * 0.6), cap,
                         f"says '{', '.join(stages[:2])}', which may include {sender.stage}")
    return Criterion("stage", cap // 3, cap, f"stage unclear: {', '.join(stages[:3])}")


def score_sector(profile: dict, sender: SenderProfile) -> Criterion:
    cap = WEIGHTS["sector"]
    theirs = normalise_all(profile.get("sectors") or [], canonical_sector)
    ours = normalise_all(sender.sectors, canonical_sector)
    if not theirs:
        return Criterion("sector", cap // 2, cap, "site does not state sectors")
    if not ours:
        return Criterion("sector", cap // 2, cap, "sender profile lists no sectors")
    overlap = theirs & ours
    if not overlap:
        return Criterion("sector", 0, cap, f"no overlap with {', '.join(sorted(theirs)[:4])}")
    share = len(overlap) / len(ours)
    return Criterion(
        "sector", round(cap * min(1.0, share + 0.34)), cap,
        f"overlaps on {', '.join(sorted(overlap))}",
    )


def score_geography(profile: dict, sender: SenderProfile) -> Criterion:
    cap = WEIGHTS["geography"]
    geos = profile.get("geographies") or []
    if not geos:
        return Criterion("geography", cap // 2, cap, "site does not state geography")
    for geo in geos:
        if geo_covers(geo, sender.geography):
            return Criterion("geography", cap, cap, f"covers {sender.geography} via '{geo}'")
    return Criterion("geography", 0, cap, f"does not cover {sender.geography}")


def score_cheque(profile: dict, sender: SenderProfile) -> Criterion:
    cap = WEIGHTS["cheque"]
    minimum = profile.get("cheque_min_usd")
    if minimum is None:
        return Criterion("cheque", cap // 2, cap, "cheque size not stated")
    if sender.raise_target_usd and minimum > sender.raise_target_usd:
        return Criterion(
            "cheque", 0, cap,
            f"minimum ${minimum:,} exceeds the whole raise of ${sender.raise_target_usd:,}",
        )
    return Criterion("cheque", cap, cap, f"minimum ${minimum:,} fits")


def score_activity(profile: dict, sender: SenderProfile) -> Criterion:
    cap = WEIGHTS["activity"]
    when = _parse_when(profile.get("last_deal_at"))
    if when is None:
        # Most sites do not date their deals. Absence of evidence is not
        # evidence of a dead fund, so this is partial rather than zero.
        return Criterion("activity", cap // 2, cap, "no dated deal found")
    age = datetime.now(timezone.utc) - when
    if age <= timedelta(days=365):
        return Criterion("activity", cap, cap, f"deal within 12 months ({profile['last_deal_at']})")
    if age <= timedelta(days=730):
        return Criterion("activity", cap // 2, cap, f"last dated deal {profile['last_deal_at']}")
    return Criterion("activity", 0, cap, f"nothing since {profile['last_deal_at']}")


def score_conflict(profile: dict, sender: SenderProfile) -> Criterion:
    cap = WEIGHTS["conflict"]
    if not sender.competitors:
        return Criterion("conflict", cap, cap, "no competitors listed to check")
    names = " | ".join(
        (c.get("name") or "").lower() for c in (profile.get("portfolio") or [])
    )
    for competitor in sender.competitors:
        needle = competitor.lower().strip()
        if needle and re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", names):
            return Criterion("conflict", CONFLICT_PENALTY, cap, f"holds {competitor}")
    return Criterion("conflict", cap, cap, "no portfolio conflict found")


def assess(profile: dict, sender: SenderProfile) -> Verdict:
    criteria = [
        score_stage(profile, sender),
        score_sector(profile, sender),
        score_geography(profile, sender),
        score_cheque(profile, sender),
        score_activity(profile, sender),
        score_conflict(profile, sender),
    ]
    total = max(0, min(100, sum(c.points for c in criteria)))
    by_name = {c.name: c for c in criteria}

    # Hard gates, applied after scoring so the breakdown still explains
    # itself, but before the verdict so a high total cannot mask them.
    for gate in ("stage", "geography"):
        if by_name[gate].zeroed:
            return Verdict(total, "rejected", criteria, by_name[gate].note)

    if by_name["conflict"].points < 0:
        return Verdict(total, "needs_review", criteria, by_name["conflict"].note)

    # Activity is worth only 10 points, so a fund dormant for years still
    # clears 70 on sector and geography alone. That is dead capital
    # dressed as a good fit. Zero here means we found a date and it is
    # old — an unknown date scores partial — so it is evidence, and a
    # person should look before anyone spends a send on it.
    if by_name["activity"].zeroed:
        return Verdict(total, "needs_review", criteria, by_name["activity"].note)

    if total >= QUALIFY_AT:
        return Verdict(total, "qualified", criteria)
    if total >= REVIEW_AT:
        weakest = min(criteria, key=lambda c: c.points / max(1, c.max_points))
        return Verdict(total, "needs_review", criteria, f"weakest: {weakest.note}")
    weakest = min(criteria, key=lambda c: c.points / max(1, c.max_points))
    return Verdict(total, "rejected", criteria, weakest.note)


def route(profile: dict, has_email: bool) -> tuple[str, str | None]:
    """How to approach, which overrides whether to.

    Order matters. A published form beats a found address: if a fund
    tells you how it wants to be approached, that is the answer.
    """
    application_url = profile.get("application_url")
    if application_url:
        return "form", str(application_url)
    if profile.get("accepts_cold") is False:
        return "intro", "site states it does not accept unsolicited approaches"
    if has_email:
        return "email", None
    return "intro", "no published address and no application form"


def qualify_campaign(campaign_name: str, only: list[str] | None = None) -> list[tuple[str, Verdict, str]]:
    """Assess every enriched target in a campaign. Idempotent."""
    out: list[tuple[str, Verdict, str]] = []
    with session() as s:
        campaign = s.scalar(select(Campaign).where(Campaign.name == campaign_name))
        if campaign is None:
            raise LookupError(f"no campaign named {campaign_name!r}")
        sender = SenderProfile.from_dict(campaign.sender_profile or {})

        stmt = select(Target).where(Target.enriched_at.is_not(None))
        if only:
            stmt = stmt.where(Target.domain.in_(only))

        for target in s.scalars(stmt):
            profile = target.profile or {}
            verdict = assess(profile, sender)

            has_email = s.scalar(
                select(Contact.id).where(
                    Contact.target_id == target.id, Contact.email.is_not(None)
                ).limit(1)
            ) is not None
            channel, note = route(profile, has_email)
            target.channel = channel
            target.channel_note = note

            existing = s.scalar(
                select(Assessment).where(
                    Assessment.target_id == target.id,
                    Assessment.campaign_id == campaign.id,
                )
            )
            if existing is None:
                existing = Assessment(target_id=target.id, campaign_id=campaign.id)
                s.add(existing)
            existing.score = verdict.score
            existing.verdict = verdict.verdict
            existing.reasons = verdict.as_json()

            out.append((target.domain, verdict, channel))
    return out
