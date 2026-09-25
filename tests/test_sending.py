"""The rules that decide whether an email is allowed to leave.

Everything else in Cohandle can be run again. A send cannot, so these
test the refusals rather than the happy path: not armed, out of hours,
over cap, no recipient. The first one matters most. Credentials are not
permission, and a draft approved on a machine with a working Zoho token
must still not go anywhere until somebody deliberately arms it.

No network and no database. `counts` is replaced, and the Zoho client is
never given a chance to reach httpx.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import sending, zoho
from app.config import settings

LAGOS = ZoneInfo("Africa/Lagos")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=LAGOS)


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    """A configured, armed, empty day unless a test says otherwise."""
    cfg = settings()
    monkeypatch.setattr(cfg, "send_enabled", True, raising=False)
    monkeypatch.setattr(cfg, "send_window_start", "08:30", raising=False)
    monkeypatch.setattr(cfg, "send_window_end", "17:30", raising=False)
    monkeypatch.setattr(cfg, "send_timezone", "Africa/Lagos", raising=False)
    monkeypatch.setattr(cfg, "send_daily_cap", 30, raising=False)
    monkeypatch.setattr(cfg, "send_hourly_cap", 6, raising=False)
    monkeypatch.setattr(cfg, "zoho_account_id", "123", raising=False)
    monkeypatch.setattr(cfg, "zoho_from_address", "hello@ciit.africa", raising=False)
    monkeypatch.setattr(sending, "counts", lambda now: (0, 0))
    return cfg


# ---------------------------------------------------------------- arming

def test_not_armed_stops_everything(armed, monkeypatch):
    monkeypatch.setattr(armed, "send_enabled", False)
    decision = sending.may_send(at(10))
    assert not decision.ok
    assert "SEND_ENABLED" in decision.why


def test_the_client_refuses_even_if_called_directly(armed, monkeypatch):
    """The last line of defence, below the discipline layer."""
    monkeypatch.setattr(armed, "send_enabled", False)
    with pytest.raises(zoho.NotArmed):
        zoho.send("someone@fund.com", "Subject", "Body")


def test_no_recipient_is_never_guessed(armed):
    for bad in ("", "   ", "not-an-address"):
        with pytest.raises(zoho.ZohoError):
            zoho.send(bad, "Subject", "Body")


# ---------------------------------------------------------------- window

def test_inside_the_window(armed):
    assert sending.may_send(at(10)).ok


@pytest.mark.parametrize("hour", [7, 8, 18, 23])
def test_outside_the_window(armed, hour):
    assert not sending.may_send(at(hour)).ok


def test_the_edges_are_inclusive(armed):
    assert sending.in_window(at(8, 30))
    assert sending.in_window(at(17, 30))
    assert not sending.in_window(at(8, 29))
    assert not sending.in_window(at(17, 31))


def test_before_hours_waits_for_this_morning(armed):
    assert sending.next_opening(at(6)) == at(8, 30)


def test_after_hours_waits_for_tomorrow(armed):
    assert sending.next_opening(at(20)) == at(8, 30) + timedelta(days=1)


def test_during_hours_means_now(armed):
    assert sending.next_opening(at(10)) == at(10)


def test_a_held_send_is_told_when_to_come_back(armed):
    decision = sending.may_send(at(6))
    assert decision.not_before == at(8, 30)


# ------------------------------------------------------------------ caps

def test_daily_cap(armed, monkeypatch):
    monkeypatch.setattr(sending, "counts", lambda now: (30, 0))
    decision = sending.may_send(at(10))
    assert not decision.ok
    assert "daily cap" in decision.why
    # Tomorrow, not in fifteen minutes.
    assert decision.not_before.date() > at(10).date()


def test_hourly_cap_waits_a_short_while(armed, monkeypatch):
    monkeypatch.setattr(sending, "counts", lambda now: (10, 6))
    decision = sending.may_send(at(10))
    assert not decision.ok
    assert "hourly cap" in decision.why
    assert decision.not_before == at(10) + timedelta(minutes=15)


def test_one_under_the_cap_still_goes(armed, monkeypatch):
    monkeypatch.setattr(sending, "counts", lambda now: (29, 5))
    assert sending.may_send(at(10)).ok


# ------------------------------------------------------------- signature

class FakeCampaign:
    def __init__(self, sender_profile):
        self.sender_profile = sender_profile


class FakeDraft:
    def __init__(self, body):
        self.body = body


def test_signature_is_appended_at_send_not_stored():
    draft = FakeDraft("The email.")
    campaign = FakeCampaign({"signature": "Yakubu\nConnected Intelligence"})
    body = sending.full_body(draft, campaign)
    assert body.startswith("The email.")
    assert body.rstrip().endswith("Connected Intelligence")


def test_no_signature_configured_is_not_an_error():
    assert sending.full_body(FakeDraft("The email."), FakeCampaign({})) \
        == "The email.\n"


def test_no_campaign_at_all():
    assert sending.full_body(FakeDraft("Body."), None) == "Body.\n"


# ------------------------------------------------------------------ zoho

def test_region_is_taken_from_the_mail_host(monkeypatch):
    cfg = settings()
    for base, expected in [("https://mail.zoho.com", "https://accounts.zoho.com"),
                           ("https://mail.zoho.eu", "https://accounts.zoho.eu"),
                           ("https://mail.zoho.in", "https://accounts.zoho.in")]:
        monkeypatch.setattr(cfg, "zoho_api_base", base, raising=False)
        assert zoho.accounts_base() == expected


def test_a_token_about_to_expire_is_not_fresh():
    import time

    assert zoho.Token("abc", time.time() + 3600).fresh
    # Inside the minute of slack, so due for refresh.
    assert not zoho.Token("abc", time.time() + 30).fresh
    assert not zoho.Token("", time.time() + 3600).fresh


# ------------------------------------------------- zoho error shapes

class FakeResponse:
    """Just enough httpx.Response for _json."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is _INVALID:
            raise ValueError("not json")
        return self._payload


_INVALID = object()


def test_a_list_shaped_error_is_reported_not_crashed():
    """Zoho answers some endpoints with [2, {...}] instead of an object.

    Assuming a dict turned INVALID_OAUTHSCOPE into an AttributeError
    several layers from the cause.
    """
    payload = [2, {"msg": "Error while processing!",
                   "errorCode": "INVALID_OAUTHSCOPE", "status": "401"}]
    with pytest.raises(zoho.ZohoError) as caught:
        zoho._json(FakeResponse(payload))
    assert "INVALID_OAUTHSCOPE" in str(caught.value)


def test_a_normal_object_still_passes_through():
    assert zoho._json(FakeResponse({"data": {"messageId": "1"}})) \
        == {"data": {"messageId": "1"}}


def test_a_body_that_is_not_json_says_so():
    with pytest.raises(zoho.ZohoError) as caught:
        zoho._json(FakeResponse(_INVALID, status_code=502, text="<html>nope"))
    assert "502" in str(caught.value)


def test_the_signature_comes_from_the_campaign():
    """Zoho does not add it to API mail, proven by a delivered test."""
    campaign = FakeCampaign({"signature": "Yinusa Yakubu Temitope\nwww.ciit.africa"})
    body = sending.full_body(FakeDraft("The email."), campaign)
    assert body.splitlines()[-1] == "www.ciit.africa"
    # One blank line between the email and the signature, not none.
    assert "The email.\n\nYinusa" in body
