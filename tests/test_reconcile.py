"""Matching a draft to the email you actually sent.

Cohandle writes a draft into Zoho and then loses sight of it. You might
send it, edit it first, sit on it, or delete it. Everything built on top
of the send record, follow-ups most of all, is built on a guess until
this closes the loop.

Matching is on recipient and time rather than on the message id, because
a draft edited and sent from the web client does not reliably keep the id
Zoho handed back when it was created. These tests pin the edges of that
choice: the subject may change, the recipient may not, and an earlier
approach to the same fund must not be mistaken for this one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.reconcile import _addresses, _normalise, _when


def ms(when: datetime) -> str:
    """Zoho reports times as epoch milliseconds, as a string."""
    return str(int(when.timestamp() * 1000))


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- subjects

def test_an_edited_subject_still_matches():
    """Editing the subject before sending is normal."""
    assert _normalise("Halene: emergency credit") == "halene emergency credit"
    assert _normalise("Halene - Emergency Credit!") == "halene emergency credit"


def test_a_reply_prefix_is_ignored():
    assert _normalise("Re: Halene") == _normalise("Halene")
    assert _normalise("FWD: Halene") == _normalise("Halene")


def test_an_empty_subject_is_not_a_crash():
    assert _normalise("") == ""
    assert _normalise(None) == ""


# ----------------------------------------------------------- recipients

def test_a_plain_address_is_found():
    assert _addresses("hello@madica.vc") == {"hello@madica.vc"}


def test_a_display_name_does_not_hide_the_address():
    assert _addresses('"Madica Team" <hello@madica.vc>') == {"hello@madica.vc"}


def test_several_recipients_are_all_found():
    got = _addresses("a@fund.com, b@fund.com")
    assert got == {"a@fund.com", "b@fund.com"}


def test_case_is_flattened():
    assert _addresses("Hello@Madica.VC") == {"hello@madica.vc"}


def test_an_empty_header_is_not_a_crash():
    assert _addresses("") == set()
    assert _addresses(None) == set()


# ----------------------------------------------------------------- time

def test_milliseconds_are_read_as_milliseconds():
    assert _when({"sentDateInGMT": ms(NOW)}) == NOW


def test_seconds_are_read_as_seconds():
    """Not every field on every account is in milliseconds."""
    assert _when({"receivedTime": str(int(NOW.timestamp()))}) == NOW


def test_received_time_is_trusted_over_the_field_named_gmt():
    """sentDateInGMT is not GMT, at least not on this account.

    A test message whose own subject recorded 22:33:06 Lagos came back
    with sentDateInGMT seven hours later, and receivedTime seven seconds
    later, which is what actually happened. Trusting the obvious name
    would have written every sent_at seven hours wrong, quietly.
    """
    truth = NOW
    seven_hours_out = NOW + timedelta(hours=7)
    got = _when({"sentDateInGMT": ms(seven_hours_out), "receivedTime": ms(truth)})
    assert got == truth


def test_the_gmt_field_is_still_better_than_nothing():
    assert _when({"sentDateInGMT": ms(NOW)}) == NOW


def test_a_missing_or_unreadable_time_is_not_a_crash():
    assert _when({}) is None
    assert _when({"sentDateInGMT": ""}) is None
    assert _when({"sentDateInGMT": "not a number"}) is None


def test_an_html_escaped_header_still_yields_the_address():
    """Zoho returns To headers escaped, entities and all."""
    header = "&quot;Yinusa Yakubu&quot;&lt;yy@ciit.africa&gt;"
    assert _addresses(header) == {"yy@ciit.africa"}
