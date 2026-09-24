"""Validation tests.

These run without an API key, on purpose. The guards in `validate()` are
plain string comparisons, so the hallucinations they exist to catch can
be written down as fixtures and checked exactly.

The fake extraction below is deliberately hostile: it contains the four
mistakes a confident model actually makes.
"""

from __future__ import annotations

from app.extract import (
    ExtractedContact,
    ExtractedFact,
    ExtractedProfile,
    Extraction,
    build_prompt,
    validate,
)

PAGES = {
    "https://example.vc/": (
        "Example Ventures backs pre-seed founders in Africa. "
        "We write first cheques of $150,000."
    ),
    "https://example.vc/portfolio": (
        "Our portfolio includes Kola Energy, a Lagos metering company, "
        "and Riverbank, a Kenyan payments business."
    ),
    "https://example.vc/contact": (
        "Reach the team at hello@example.vc. Press enquiries: press@example.vc."
    ),
}


def _extraction(**kw) -> Extraction:
    base = dict(
        profile=ExtractedProfile(name="Example Ventures"),
        facts=[],
        contacts=[],
    )
    base.update(kw)
    return Extraction(**base)


def test_fact_from_a_crawled_page_is_kept():
    ex = _extraction(facts=[
        ExtractedFact(
            kind="portfolio",
            claim="Backed Kola Energy, a Lagos metering company.",
            source_url="https://example.vc/portfolio",
        )
    ])
    _, report = validate(ex, PAGES)
    assert len(report.kept_facts) == 1
    assert not report.dropped


def test_fact_citing_a_page_we_never_crawled_is_dropped():
    """The commonest failure: a plausible URL the model made up."""
    ex = _extraction(facts=[
        ExtractedFact(
            kind="deal",
            claim="Led a $2m round in 2026.",
            source_url="https://example.vc/news/2026-round",
        )
    ])
    _, report = validate(ex, PAGES)
    assert report.kept_facts == []
    assert "not a crawled page" in report.dropped[0][1]


def test_invented_email_is_dropped():
    """A fabricated address is the one that gets the email bounced."""
    ex = _extraction(contacts=[
        ExtractedContact(email="partners@example.vc", source_url="https://example.vc/contact")
    ])
    _, report = validate(ex, PAGES)
    assert report.kept_contacts == []
    assert "does not appear on that page" in report.dropped[0][1]


def test_real_email_on_the_right_page_is_kept():
    ex = _extraction(contacts=[
        ExtractedContact(email="hello@example.vc", source_url="https://example.vc/contact")
    ])
    _, report = validate(ex, PAGES)
    assert [c.email for c in report.kept_contacts] == ["hello@example.vc"]


def test_email_cited_to_the_wrong_page_is_dropped():
    """Right address, wrong source. Still wrong: the citation must hold."""
    ex = _extraction(contacts=[
        ExtractedContact(email="hello@example.vc", source_url="https://example.vc/portfolio")
    ])
    _, report = validate(ex, PAGES)
    assert report.kept_contacts == []


def test_a_paraphrase_is_demoted_rather_than_binned():
    """Close is not good enough for quotation marks, but it is still a
    fair summary and a person vetting this would rather see it. It moves
    to observations, which are never quoted."""
    ex = _extraction(profile=ExtractedProfile(
        thesis_quote="We back pre-seed founders across the African continent."
    ))
    profile, report = validate(ex, PAGES)
    assert profile.thesis_quote is None
    assert any("African continent" in o for o in profile.observations)
    assert any("not verbatim" in why for _, why in report.dropped)


def test_verbatim_thesis_quote_survives():
    ex = _extraction(profile=ExtractedProfile(
        thesis_quote="backs pre-seed founders in Africa"
    ))
    profile, _ = validate(ex, PAGES)
    assert profile.thesis_quote == "backs pre-seed founders in Africa"


def test_thesis_quote_ignores_whitespace_and_case():
    ex = _extraction(profile=ExtractedProfile(
        thesis_quote="Backs   Pre-Seed\n founders in AFRICA"
    ))
    profile, _ = validate(ex, PAGES)
    assert profile.thesis_quote is not None


def test_trailing_slash_does_not_break_citation():
    ex = _extraction(facts=[
        ExtractedFact(kind="thesis", claim="Writes first cheques of $150,000.",
                      source_url="https://example.vc")
    ])
    _, report = validate(ex, PAGES)
    assert len(report.kept_facts) == 1


def test_duplicate_claims_collapse():
    same = "Backed Kola Energy, a Lagos metering company."
    ex = _extraction(facts=[
        ExtractedFact(kind="portfolio", claim=same, source_url="https://example.vc/portfolio"),
        ExtractedFact(kind="portfolio", claim=same.upper(), source_url="https://example.vc/portfolio"),
    ])
    _, report = validate(ex, PAGES)
    assert len(report.kept_facts) == 1


def test_prompt_labels_every_page_with_its_url():
    """Citation is only copyable if the URL is in front of the model."""
    prompt = build_prompt("example.vc", PAGES)
    for url in PAGES:
        assert f"--- PAGE: {url} ---" in prompt


def test_prompt_is_bounded():
    huge = {f"https://example.vc/p{i}": "x" * 50_000 for i in range(20)}
    assert len(build_prompt("example.vc", huge)) <= 61_000
