"""Keeping the page somebody actually pasted.

Ingest keys on domain, which is right for deduplication and wrong for
crawling. Pasting https://www.ventra.global/ventra-capital produced a
target for ventra.global and then crawled the homepage, the about page
and the contact page. The one page named explicitly was the one page
never fetched, so the read described an AI product rather than the fund,
and the application form was missed entirely.
"""

from __future__ import annotations

from app.ingest import _seed_url, parse_entry


def test_a_pasted_page_is_kept():
    entry = parse_entry("https://www.ventra.global/ventra-capital")
    assert entry.domain == "ventra.global"
    assert entry.seed_url == "https://www.ventra.global/ventra-capital"


def test_a_bare_domain_has_nothing_to_keep():
    assert parse_entry("ventra.global").seed_url is None


def test_a_link_to_the_root_has_nothing_to_keep():
    """The crawler starts at the root anyway."""
    assert parse_entry("https://www.madica.vc/").seed_url is None
    assert parse_entry("https://www.madica.vc").seed_url is None


def test_a_query_string_survives():
    """Some application pages are only reachable with one."""
    assert _seed_url("https://fund.com/apply?form=seed") \
        == "https://fund.com/apply?form=seed"


def test_a_fragment_is_dropped():
    """It never reaches the server, so it is noise in a stored url."""
    assert _seed_url("https://fund.com/apply#section") == "https://fund.com/apply"


def test_http_is_upgraded():
    assert _seed_url("http://fund.com/apply").startswith("https://")


def test_rubbish_does_not_become_a_seed():
    assert _seed_url("not a url at all") is None
