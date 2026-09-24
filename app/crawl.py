"""Crawl.

Fetches the handful of pages on a target's site that actually carry
facts, and stores the extracted text.

Not a general web crawler. It looks for seven specific kinds of page,
takes at most a dozen, and stops. The goal is enough grounded material
for one email, not a copy of the site.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from selectolax.parser import HTMLParser
from sqlalchemy import select

from app.config import settings
from app.db import session
from app.models import Page, Target

USER_AGENT = (
    "Mozilla/5.0 (compatible; Cohandle/1.0; +https://ciit.africa) research crawler"
)

MAX_PAGES = 12
MAX_HTML_BYTES = 2_000_000
PER_HOST_DELAY = 1.0          # seconds between requests to one host
REQUEST_TIMEOUT = 15.0
# Below this, a fetch produced nothing worth extracting. In practice that
# means a client-rendered shell rather than a genuinely empty page.
MIN_USEFUL_CHARS = 250

# Ordered: earlier kinds are worth more than later ones when picking
# which dozen links to follow.
PAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("thesis", re.compile(r"thesis|approach|how-we-invest|what-we-(look|back)|philosophy|focus|strategy", re.I)),
    ("portfolio", re.compile(r"portfolio|companies|investments|our-companies|founders-we", re.I)),
    ("apply", re.compile(r"apply|submit|pitch|for-founders|get-funded|raise", re.I)),
    ("team", re.compile(r"team|people|partners|who-we-are|leadership", re.I)),
    ("about", re.compile(r"about|our-story|mission|why", re.I)),
    ("contact", re.compile(r"contact|get-in-touch|reach-us", re.I)),
]

SKIP_URL = re.compile(
    r"\.(pdf|zip|jpg|jpeg|png|gif|svg|webp|mp4|mp3|doc|docx|xls|xlsx|ppt|pptx)$"
    r"|/(privacy|terms|cookie|legal|disclaimer|sitemap|feed|rss|tag|category|author)"
    r"|^mailto:|^tel:|^javascript:|#",
    re.I,
)


@dataclass
class Fetched:
    url: str
    status: int | None
    html: str
    error: str | None = None


@dataclass
class CrawlReport:
    domain: str
    pages: int = 0
    chars: int = 0
    needs_render: int = 0
    errors: list[str] | None = None
    blocked_by_robots: bool = False

    def __str__(self) -> str:
        if self.blocked_by_robots:
            return f"{self.domain}: blocked by robots.txt"
        bits = [f"{self.domain}: {self.pages} pages, {self.chars:,} chars"]
        if self.needs_render:
            bits.append(f"{self.needs_render} need rendering")
        if self.errors:
            bits.append(f"{len(self.errors)} errors")
        return ", ".join(bits)


async def _robots(client: httpx.AsyncClient, base: str) -> RobotFileParser | None:
    """Fetch and parse robots.txt. A missing file means no restriction."""
    parser = RobotFileParser()
    try:
        resp = await client.get(urljoin(base, "/robots.txt"), timeout=8.0)
        if resp.status_code >= 400:
            parser.parse([])
            return parser
        parser.parse(resp.text.splitlines())
        return parser
    except Exception:
        # Unreachable robots.txt is not consent, but neither is it a
        # prohibition. Treat as unrestricted and stay within the page cap.
        parser.parse([])
        return parser


async def _fetch(client: httpx.AsyncClient, url: str) -> Fetched:
    try:
        resp = await client.get(url, timeout=REQUEST_TIMEOUT)
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return Fetched(url, resp.status_code, "", error=f"not html ({ctype})")
        return Fetched(str(resp.url), resp.status_code, resp.text[:MAX_HTML_BYTES])
    except Exception as exc:
        return Fetched(url, None, "", error=f"{type(exc).__name__}: {exc}")


def _classify(href: str, text: str) -> str | None:
    hay = f"{href} {text}"
    for kind, pattern in PAGE_PATTERNS:
        if pattern.search(hay):
            return kind
    return None


def discover(home_url: str, html: str) -> dict[str, str]:
    """Pick the pages worth fetching, one URL per kind.

    Same registrable host only. A link out to a portfolio company's own
    site is interesting later, but it is not this target's page.
    """
    host = urlsplit(home_url).hostname or ""
    root = host[4:] if host.startswith("www.") else host
    found: dict[str, str] = {}

    for node in HTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or SKIP_URL.search(href):
            continue
        absolute = urljoin(home_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        link_host = (parts.hostname or "").removeprefix("www.")
        if link_host != root:
            continue
        kind = _classify(parts.path, node.text(strip=True) or "")
        if kind and kind not in found:
            found[kind] = absolute.split("#")[0]
    return found


def extract(html: str, url: str) -> str:
    """Main content as plain text.

    trafilatura first: it is markedly better at ignoring nav, cookie
    banners and footers. The selectolax fallback catches pages it
    declines entirely, which tends to be very short ones.
    """
    text = trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True, favor_precision=True
    )
    if text and len(text) >= MIN_USEFUL_CHARS:
        return text.strip()

    tree = HTMLParser(html)
    for tag in ("script", "style", "nav", "footer", "header", "noscript", "svg"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    fallback = body.text(separator=" ", strip=True) if body else ""
    fallback = re.sub(r"\s{2,}", " ", fallback).strip()
    return (text or "").strip() if len(text or "") > len(fallback) else fallback


async def crawl_domain(domain: str) -> CrawlReport:
    """Fetch and store the interesting pages for one domain."""
    report = CrawlReport(domain=domain, errors=[])
    base = f"https://{domain}/"

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, http2=False
    ) as client:
        robots = await _robots(client, base)

        if robots and not robots.can_fetch(USER_AGENT, base):
            report.blocked_by_robots = True
            return report

        home = await _fetch(client, base)
        if home.error or not home.html:
            # https may simply not be served; try http once before giving up.
            home = await _fetch(client, f"http://{domain}/")
        if home.error or not home.html:
            report.errors.append(f"home: {home.error or 'empty'}")
            return report

        targets: list[tuple[str, str]] = [("home", home.url)]
        for kind, url in discover(home.url, home.html).items():
            if len(targets) >= MAX_PAGES:
                break
            targets.append((kind, url))

        results: list[tuple[str, Fetched]] = [("home", home)]
        for kind, url in targets[1:]:
            if robots and not robots.can_fetch(USER_AGENT, url):
                continue
            # Serial with a delay rather than concurrent: one polite
            # crawler on somebody's marketing site, not a thundering herd.
            await asyncio.sleep(PER_HOST_DELAY)
            results.append((kind, await _fetch(client, url)))

    now = datetime.now(timezone.utc)
    with session() as s:
        target = s.scalar(select(Target).where(Target.domain == domain))
        if target is None:
            report.errors.append("no target row; ingest it first")
            return report

        for kind, got in results:
            if got.error:
                report.errors.append(f"{kind}: {got.error}")
                continue

            text = extract(got.html, got.url)
            needs_render = len(text) < MIN_USEFUL_CHARS

            page = s.scalar(
                select(Page).where(Page.target_id == target.id, Page.url == got.url)
            )
            if page is None:
                page = Page(target_id=target.id, url=got.url)
                s.add(page)
            page.kind = kind
            page.http_status = got.status
            page.text = text or None
            page.text_chars = len(text)
            page.needs_render = needs_render
            page.rendered = False
            page.fetched_at = now
            page.error = None

            report.pages += 1
            report.chars += len(text)
            report.needs_render += int(needs_render)

    return report


async def crawl_many(domains: list[str], concurrency: int = 4) -> list[CrawlReport]:
    """Crawl several domains at once, one request at a time within each."""
    gate = asyncio.Semaphore(concurrency)

    async def one(domain: str) -> CrawlReport:
        async with gate:
            try:
                return await crawl_domain(domain)
            except Exception as exc:
                return CrawlReport(domain=domain, errors=[f"{type(exc).__name__}: {exc}"])

    return await asyncio.gather(*(one(d) for d in domains))
