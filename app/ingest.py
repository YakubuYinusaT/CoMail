"""Ingest.

Takes whatever gets pasted in — URLs, email addresses, one per line or
separated by commas — and turns it into `target` rows keyed on domain.

Deliberately forgiving about input format and strict about what it
stores: a domain that cannot be parsed is reported back rather than
guessed at, because a wrong domain means crawling a stranger's website.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from app.db import session
from app.models import Contact, Target

# Hosts that are never the target itself. Someone pasting a LinkedIn or
# Crunchbase page means the fund, but crawling those gets a login wall
# and, for the social ones, a terms-of-service problem.
NOT_A_TARGET = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "crunchbase.com", "angel.co", "wellfound.com", "medium.com",
    "youtube.com", "github.com", "docs.google.com", "forms．gle", "forms.gle",
    "notion.so", "substack.com", "calendly.com",
}

# Free mail hosts: the domain says nothing about the organisation, so an
# address there gives us a contact but no site to crawl.
FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


@dataclass
class ParsedEntry:
    raw: str
    domain: str | None = None
    email: str | None = None
    problem: str | None = None
    # The pasted address when it named a page rather than a bare site.
    seed_url: str | None = None


@dataclass
class IngestResult:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    contacts_added: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.created)} new, {len(self.existing)} already known, "
            f"{len(self.contacts_added)} contacts, {len(self.rejected)} rejected"
        )


def normalise_domain(value: str) -> str | None:
    """Reduce anything host-shaped to a bare, lowercase registrable host.

    Returns None rather than a guess when the input is not a domain.
    Being wrong here means crawling somebody else's website.
    """
    text = (value or "").strip().strip("<>\"'").rstrip(".,;")
    if not text:
        return None

    if "@" in text and not text.startswith("http"):
        text = text.rsplit("@", 1)[-1]

    if "//" not in text:
        text = "//" + text
    host = urlsplit(text).hostname or ""
    host = host.lower().rstrip(".")

    if host.startswith("www."):
        host = host[4:]
    if not host or not DOMAIN_RE.match(host):
        return None
    # A bare TLD or a single label is not a site.
    if host.count(".") == 0:
        return None
    # No TLD is numeric, so an all-digit last label means this is an IP
    # address. Refuse them: a pasted 192.168.x.x or 169.254.169.254 would
    # otherwise point the crawler at a private host or a cloud metadata
    # endpoint, which is somebody else's incident report.
    if host.rsplit(".", 1)[-1].isdigit():
        return None
    return host


def _seed_url(text: str) -> str | None:
    """The pasted address, when it pointed at a particular page.

    Somebody pasting /ventra-capital means that page. Keying on domain
    is right for deduplication and wrong for crawling: the one page they
    named was the one page never fetched.

    A bare domain or a link to the root gives nothing, because the
    crawler starts there anyway.
    """
    candidate = text if "://" in text else f"https://{text}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if not parts.netloc:
        return None
    path = (parts.path or "").strip("/")
    if not path:
        return None
    rebuilt = urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
    return rebuilt[:2000]


def parse_entry(raw: str) -> ParsedEntry:
    text = raw.strip()
    if not text:
        return ParsedEntry(raw=raw, problem="empty")

    email = text.lower() if EMAIL_RE.match(text) else None
    domain = normalise_domain(text)

    if domain is None:
        return ParsedEntry(raw=raw, email=email, problem="not a domain or email")

    # Match the registrable domain, so mail.linkedin.com is caught too.
    for blocked in NOT_A_TARGET:
        if domain == blocked or domain.endswith("." + blocked):
            return ParsedEntry(raw=raw, domain=domain, email=email,
                               problem=f"{blocked} is a profile, not the organisation's own site")

    if email and domain in FREE_MAIL:
        return ParsedEntry(raw=raw, email=email,
                           problem="free mail address, no organisation site to crawl")

    return ParsedEntry(raw=raw, domain=domain, email=email,
                       seed_url=_seed_url(text))


def parse_input(blob: str) -> list[ParsedEntry]:
    """Split a pasted blob on newlines, commas, semicolons or whitespace."""
    tokens = [t for t in re.split(r"[\s,;]+", blob or "") if t.strip()]
    seen: set[str] = set()
    out: list[ParsedEntry] = []
    for token in tokens:
        entry = parse_entry(token)
        key = entry.email or entry.domain or entry.raw
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def ingest(blob: str, kind: str = "vc") -> IngestResult:
    """Create targets for everything parseable. Idempotent on domain."""
    result = IngestResult()
    entries = parse_input(blob)

    with session() as s:
        for entry in entries:
            if entry.problem or not entry.domain:
                result.rejected.append((entry.raw, entry.problem or "unparseable"))
                continue

            target = s.scalar(select(Target).where(Target.domain == entry.domain))
            if target is None:
                target = Target(domain=entry.domain, kind=kind,
                                seed_url=entry.seed_url)
                s.add(target)
                s.flush()
                result.created.append(entry.domain)
            else:
                # A later paste naming a specific page is new information
                # about a target we already had, so take it.
                if entry.seed_url and not target.seed_url:
                    target.seed_url = entry.seed_url
                result.existing.append(entry.domain)

            if entry.email:
                known = s.scalar(
                    select(Contact).where(
                        Contact.target_id == target.id, Contact.email == entry.email
                    )
                )
                if known is None:
                    # source_url is the pasted list itself: a person
                    # supplied this address, and the schema will not hold
                    # an address without saying where it came from.
                    s.add(
                        Contact(
                            target_id=target.id,
                            email=entry.email,
                            verified=True,
                            source_url="operator:pasted-list",
                        )
                    )
                    result.contacts_added.append(entry.email)

    return result
