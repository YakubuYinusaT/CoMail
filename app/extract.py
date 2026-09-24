"""Extraction.

Turns crawled page text into a structured profile and a ledger of facts,
each carrying the URL it came from.

The model is not trusted. Everything it returns passes through
`validate()` first, and the checks there are ordinary string comparisons
against text we actually fetched — no second model call, no judgement.
A claim that fails is dropped, not repaired.

This matters more than it sounds. Naming a portfolio company a fund does
not hold, in an email to that fund, does not get a polite no. The guards
below are what stand between a confident model and that outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.config import settings
from app.db import session
from app.models import Contact, Fact, Page, Target

FactKind = Literal["portfolio", "thesis", "deal", "publication", "person", "cheque_size"]
Confidence = Literal["verified", "inferred"]

MAX_CLAIM_CHARS = 300
MAX_PAGE_CHARS = 12_000      # per page, into the prompt
MAX_PROMPT_CHARS = 60_000    # across all pages for one target
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


# --------------------------------------------------------------------------
# what the model must return
# --------------------------------------------------------------------------

class PortfolioCompany(BaseModel):
    name: str = Field(max_length=200)
    one_line: str | None = Field(default=None, max_length=300)


class ExtractedProfile(BaseModel):
    """Structured summary. Feeds qualification, never quoted directly."""

    name: str | None = Field(default=None, max_length=300)
    stages: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    cheque_min_usd: int | None = None
    cheque_max_usd: int | None = None
    portfolio: list[PortfolioCompany] = Field(default_factory=list)
    thesis_quote: str | None = Field(default=None, max_length=500)
    last_deal_at: str | None = Field(default=None, max_length=40)
    # None means the site does not say. Only false when it says so.
    accepts_cold: bool | None = None
    application_url: str | None = None


class ExtractedFact(BaseModel):
    kind: FactKind
    claim: str = Field(max_length=MAX_CLAIM_CHARS)
    source_url: str
    confidence: Confidence = "verified"

    @field_validator("claim")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("claim is empty")
        return v.strip()


class ExtractedContact(BaseModel):
    email: str
    name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    source_url: str


class Extraction(BaseModel):
    profile: ExtractedProfile
    facts: list[ExtractedFact] = Field(default_factory=list)
    contacts: list[ExtractedContact] = Field(default_factory=list)


# --------------------------------------------------------------------------
# validation: cheap, offline, and the whole point
# --------------------------------------------------------------------------

@dataclass
class ValidationReport:
    kept_facts: list[ExtractedFact] = field(default_factory=list)
    kept_contacts: list[ExtractedContact] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    def note(self, what: str, why: str) -> None:
        self.dropped.append((what[:90], why))


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _norm_text(text: str) -> str:
    """Whitespace-insensitive, case-insensitive, for substring checks."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def validate(
    extraction: Extraction, pages: dict[str, str]
) -> tuple[ExtractedProfile, ValidationReport]:
    """Drop anything not supported by the text we actually fetched.

    `pages` maps URL to extracted text. Three checks, all of them plain
    string work:

    1. A source URL must be a page we crawled. The model cannot cite a
       page it invented, or one it half-remembers from training.
    2. An email must appear literally in that page's text. This is the
       strongest guard available, because a fabricated address will not
       be there.
    3. A thesis quote must appear in some page's text. If we are going to
       put a fund's own words back to them, they had better be its words.
    """
    report = ValidationReport()
    allowed = {_norm_url(u): text for u, text in pages.items()}
    corpus = _norm_text(" ".join(pages.values()))

    seen: set[tuple[str, str]] = set()
    for fact in extraction.facts:
        key = _norm_url(fact.source_url)
        if key not in allowed:
            report.note(fact.claim, f"source not a crawled page: {fact.source_url}")
            continue
        dedupe = (fact.kind, _norm_text(fact.claim))
        if dedupe in seen:
            report.note(fact.claim, "duplicate")
            continue
        seen.add(dedupe)
        report.kept_facts.append(fact)

    for contact in extraction.contacts:
        key = _norm_url(contact.source_url)
        if key not in allowed:
            report.note(contact.email, f"source not a crawled page: {contact.source_url}")
            continue
        if not EMAIL_RE.fullmatch(contact.email.strip()):
            report.note(contact.email, "not a valid address")
            continue
        if contact.email.lower() not in allowed[key].lower():
            report.note(contact.email, "address does not appear on that page")
            continue
        report.kept_contacts.append(contact)

    profile = extraction.profile.model_copy(deep=True)
    if profile.thesis_quote:
        if _norm_text(profile.thesis_quote) not in corpus:
            report.note(profile.thesis_quote, "thesis quote not found in any crawled page")
            profile.thesis_quote = None

    if profile.application_url and _norm_url(profile.application_url) not in allowed:
        # Not necessarily wrong — an apply page we did not crawl is still
        # an apply page — but mark it so review can see it is unverified.
        report.note(profile.application_url, "application_url was not crawled (kept, unverified)")

    return profile, report


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You read an organisation's own website and record what it says about itself.

You are building material that will be quoted back to this organisation in
a cold email. A claim that is wrong is worse than no claim at all, so the
standard is not "probably true" — it is "stated on the page".

Rules, in order of importance:

1. Every fact carries the exact source_url of the page you read it on.
   Use the URLs given in the page headers below, character for character.
2. Record only what the page states. Do not infer, extrapolate, or use
   anything you know about this organisation from elsewhere.
3. A thesis_quote must be a verbatim span from the page. Never paraphrase
   into that field.
4. An email address goes in contacts only if it appears literally on the
   page you cite.
5. Mark a fact "inferred" when the page implies it without saying it.
   Inferred facts inform scoring but are never quoted, so when in doubt,
   mark inferred rather than dropping it.
6. accepts_cold is false only where the site says it does not take
   unsolicited approaches. Absence of a statement means null, not true.

Write each claim as one plain sentence someone could check by opening the
source URL.
"""


def build_prompt(domain: str, pages: dict[str, str]) -> str:
    """Page text with explicit URL headers, so citation is copyable."""
    chunks: list[str] = [f"Organisation website: {domain}\n"]
    budget = MAX_PROMPT_CHARS
    for url, text in pages.items():
        if budget <= 0:
            break
        body = (text or "")[:MAX_PAGE_CHARS]
        body = body[:budget]
        budget -= len(body)
        chunks.append(f"\n--- PAGE: {url} ---\n{body}\n")
    return "".join(chunks)


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def _client():
    """Built lazily so the module imports without a key.

    Everything above this line is testable offline; only this needs
    credentials.
    """
    cfg = settings()
    if not cfg.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Extraction needs it; crawling and "
            "qualification do not."
        )
    import instructor
    from anthropic import Anthropic

    return instructor.from_anthropic(Anthropic(api_key=cfg.anthropic_api_key))


def call_model(domain: str, pages: dict[str, str], model: str = "claude-sonnet-5") -> Extraction:
    """One call, structured output, validated by pydantic before return."""
    return _client().messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(domain, pages)}],
        response_model=Extraction,
    )


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def load_pages(domain: str) -> dict[str, str]:
    """Crawled text for a domain, thickest pages first."""
    with session() as s:
        target = s.scalar(select(Target).where(Target.domain == domain))
        if target is None:
            raise LookupError(f"no target for {domain}; ingest it first")
        rows = s.scalars(
            select(Page)
            .where(Page.target_id == target.id, Page.text.is_not(None))
            .order_by(Page.text_chars.desc())
        ).all()
        return {p.url: p.text or "" for p in rows if p.text_chars > 0}


def store(domain: str, profile: ExtractedProfile, report: ValidationReport) -> None:
    """Replace this target's facts with the validated set."""
    now = datetime.now(timezone.utc)
    with session() as s:
        target = s.scalar(select(Target).where(Target.domain == domain))
        if target is None:
            raise LookupError(f"no target for {domain}")

        # Facts are derived data. Re-extracting replaces them rather than
        # accumulating duplicates across runs.
        for old in s.scalars(select(Fact).where(Fact.target_id == target.id)):
            s.delete(old)
        s.flush()

        for fact in report.kept_facts:
            s.add(
                Fact(
                    target_id=target.id,
                    kind=fact.kind,
                    claim=fact.claim,
                    source_url=fact.source_url,
                    confidence=fact.confidence,
                    observed_at=now,
                )
            )

        for contact in report.kept_contacts:
            email = contact.email.strip().lower()
            existing = s.scalar(
                select(Contact).where(
                    Contact.target_id == target.id, Contact.email == email
                )
            )
            if existing is None:
                s.add(
                    Contact(
                        target_id=target.id,
                        email=email,
                        name=contact.name,
                        role=contact.role,
                        source_url=contact.source_url,
                    )
                )

        if profile.name and not target.name:
            target.name = profile.name
        target.profile = profile.model_dump(mode="json")
        target.enriched_at = now


def extract_target(domain: str, model: str = "claude-sonnet-5") -> ValidationReport:
    """Crawled pages in, validated facts in the database out."""
    pages = load_pages(domain)
    if not pages:
        raise LookupError(f"no crawled pages for {domain}; run crawl first")
    raw = call_model(domain, pages, model=model)
    profile, report = validate(raw, pages)
    store(domain, profile, report)
    return report
