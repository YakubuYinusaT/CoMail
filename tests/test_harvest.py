"""Finding addresses on a page, without inventing any.

Extraction once asked the model for addresses and it produced five,
plausible and wrong, for real named staff at a real fund. The field was
removed rather than guarded.

This is the replacement, and the property that matters is structural
rather than tested by sampling: nothing here asks a model anything, so an
address that is not in the page cannot come out of it. What these tests
pin down is the other half, which is noise. A contact panel that lists
`logo@2x.png` and a Wix telemetry address teaches the operator to ignore
it, and an ignored panel is worse than no panel.
"""

from __future__ import annotations

from app.harvest import find, plausible


def test_a_mailto_link_is_found():
    """It never reaches the extracted text, only the markup."""
    assert find('<a href="mailto:hello@fund.com">Contact</a>') == ["hello@fund.com"]


def test_an_address_in_prose_is_found():
    assert "capital@fund.com" in find("", "Write to capital@fund.com about the fund.")


def test_the_same_address_twice_is_one_finding():
    html = '<a href="mailto:a@fund.com">a@fund.com</a> a@fund.com'
    assert find(html, "a@fund.com") == ["a@fund.com"]


def test_case_is_flattened():
    """The schema stores addresses lowercased."""
    assert find("<p>Hello@Fund.COM</p>") == ["hello@fund.com"]


def test_a_trailing_full_stop_is_not_part_of_it():
    assert find("", "Reach us at team@fund.com.") == ["team@fund.com"]


# ----------------------------------------------------------------- noise

def test_retina_image_names_are_not_addresses():
    for name in ("logo@2x.png", "icon@3x.svg", "hero@2x.webp"):
        assert not plausible(name), name


def test_site_builder_telemetry_is_not_their_address():
    for address in ("sentry@sentry-next.wixpress.com",
                    "abc@o123.ingest.sentry.io",
                    "x@squarespace.com"):
        assert not plausible(address), address


def test_placeholders_in_template_markup_are_dropped():
    for address in ("you@example.com", "email@domain.com", "your@yourdomain.com"):
        assert not plausible(address), address


def test_addresses_nobody_should_write_to_are_dropped():
    for address in ("noreply@fund.com", "no-reply@fund.com",
                    "postmaster@fund.com", "unsubscribe@fund.com"):
        assert not plausible(address), address


def test_an_ip_literal_is_not_a_mail_domain():
    assert not plausible("x@192.168.1.1")


def test_a_real_address_survives_all_of_it():
    for address in ("hello@madica.vc", "community@ventra.global",
                    "first.last@fund.co.uk", "info@fund-name.africa"):
        assert plausible(address), address


def test_a_page_of_assets_yields_nothing():
    """A stylesheet full of @media and @font-face is the usual false start."""
    css = "@media screen { } @font-face { src: url(a@2x.woff2); } @import x;"
    assert find(css) == []
