"""Finding email addresses on a page.

Extraction used to ask the model for addresses and it invented five, all
plausible, all for real named staff at a real fund. The field was removed
rather than guarded, and the operator supplied every address by hand.

This puts finding back without putting invention back. Nothing here asks
a model anything. Addresses are matched out of the page with a regular
expression, so every one returned is literally present in the bytes that
were served, and the page it came from is recorded with it. An address
that is not on the page cannot be produced by this code.

What it finds is still marked unverified. A regular expression cannot
tell you whether info@ reaches a person, only that the characters were
there.
"""

from __future__ import annotations

import re

# Deliberately stricter than the RFC. A permissive pattern over raw HTML
# matches half the stylesheet.
EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\b"
)

# Image and asset filenames read as addresses: logo@2x.png, icon@3x.svg.
ASSET_SUFFIX = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|avif|ico|css|js|woff2?|ttf|eot|mp4|pdf)$", re.I)

# Addresses that belong to the tools a site is built with, not to the
# organisation. Matched on the registrable domain.
NOT_THEIRS = {
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "wix.com",
    "squarespace.com", "webflow.com", "shopify.com", "godaddy.com",
    "example.com", "example.org", "domain.com", "yourdomain.com",
    "email.com", "company.com", "sentry.wixpress.com", "wordpress.com",
    "cloudflare.com", "google.com", "gstatic.com", "googleapis.com",
    "schema.org", "w3.org", "adobe.com", "typekit.com", "jquery.com",
}

# Local parts that are never a person and never worth writing to.
NOT_WORTH_IT = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "abuse", "unsubscribe", "bounce", "bounces",
}

# Placeholders that appear in template markup.
PLACEHOLDER_LOCAL = {"email", "your", "youremail", "name", "username", "user"}


def _registrable(domain: str) -> str:
    """The last two labels. Enough to spot a builder's own domain."""
    parts = domain.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower()


def plausible(address: str) -> bool:
    """Whether an address is worth showing a person.

    Rejections here are about noise, not about truth. Everything that
    survives was on the page either way.
    """
    address = address.strip().lower()
    if address.count("@") != 1 or len(address) > 254:
        return False

    local, _, domain = address.partition("@")
    if not local or not domain or ".." in address:
        return False
    if ASSET_SUFFIX.search(domain):
        return False
    if local in NOT_WORTH_IT or local in PLACEHOLDER_LOCAL:
        return False
    if _registrable(domain) in NOT_THEIRS or domain in NOT_THEIRS:
        return False
    # A version suffix is how logo@2x.png reaches this point.
    if re.fullmatch(r"\d+x", local):
        return False
    # A trailing digit-only label is an IP address, not a mail domain.
    if domain.split(".")[-1].isdigit():
        return False
    return True


def find(html: str, text: str = "") -> list[str]:
    """Every plausible address in this page, lowercased and deduplicated.

    Both the markup and the extracted text are searched. An address that
    only exists as a mailto link never reaches the text, and one written
    as an image caption never reaches the markup as a link.
    """
    found: dict[str, None] = {}
    for blob in (html or "", text or ""):
        for match in EMAIL.finditer(blob):
            address = match.group(0).lower().rstrip(".")
            if plausible(address):
                found.setdefault(address, None)
    return list(found)
