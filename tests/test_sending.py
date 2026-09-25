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
    """A configured, armed, empty day in direct-send mode.

    delivery_mode is forced to "send" because the window and the caps
    only govern delivery. In the default draft mode they are skipped, so
    a test of them has to say which mode it means.
    """
    cfg = settings()
    monkeypatch.setattr(cfg, "delivery_mode", "send", raising=False)
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


# ------------------------------------------------------------- drafting

def test_drafting_needs_no_arming(armed, monkeypatch):
    """A draft reaches nobody, so the arming switch has nothing to guard."""
    monkeypatch.setattr(armed, "delivery_mode", "draft")
    monkeypatch.setattr(armed, "send_enabled", False)
    assert sending.drafting()
    assert sending.may_send(at(3)).ok, "3am, unarmed, and still fine"


def test_drafting_ignores_the_window_and_the_caps(armed, monkeypatch):
    monkeypatch.setattr(armed, "delivery_mode", "draft")
    monkeypatch.setattr(sending, "counts", lambda now: (999, 999))
    assert sending.may_send(at(23, 59)).ok


def test_the_mode_is_read_leniently():
    cfg = settings()
    for value, expected in [("draft", True), ("DRAFT", True), (" draft ", True),
                            ("send", False), ("", False)]:
        object.__setattr__(cfg, "delivery_mode", value)
        assert sending.drafting() is expected, value
    object.__setattr__(cfg, "delivery_mode", "draft")


def test_a_draft_zoho_did_not_confirm_is_a_failure(monkeypatch):
    """If mode were ever ignored, the email would already be delivered."""
    monkeypatch.setattr(zoho, "_call", lambda *a, **k: {"data": {"mode": "send"}})
    monkeypatch.setattr(zoho, "_require", lambda *names: None)
    with pytest.raises(zoho.ZohoError) as caught:
        zoho.create_draft("someone@fund.com", "Subject", "Body")
    assert "may have been delivered" in str(caught.value)


def test_a_confirmed_draft_comes_back(monkeypatch):
    monkeypatch.setattr(zoho, "_call",
                        lambda *a, **k: {"data": {"mode": "draft", "messageId": "9"}})
    monkeypatch.setattr(zoho, "_require", lambda *names: None)
    assert zoho.create_draft("someone@fund.com", "S", "B")["messageId"] == "9"


# ----------------------------------------------------------------- html

def test_html_body_escapes_what_a_person_typed():
    from app.html_mail import body_to_html

    out = body_to_html("Costs < $250k & rising")
    assert "&lt;" in out and "&amp;" in out
    assert "<script" not in out


def test_paragraphs_survive_and_single_breaks_become_br():
    from app.html_mail import body_to_html

    out = body_to_html("One.\n\nTwo.\nStill two.")
    assert out.count("<p ") == 2
    assert "<br>" in out


def test_the_designed_signature_replaces_the_text_one(monkeypatch):
    """Two signatures read as a mistake, so HTML drops the plain one."""
    monkeypatch.setattr(sending, "as_html", lambda: True)
    monkeypatch.setattr(sending, "html_signature", lambda: "<table>designed</table>")
    campaign = FakeCampaign({"signature": "Yinusa Yakubu Temitope"})
    body, is_html = sending.message_body(FakeDraft("The email."), campaign)
    assert is_html
    assert "designed" in body
    assert "Yinusa Yakubu Temitope" not in body


def test_without_a_designed_signature_it_stays_plain(monkeypatch):
    """A missing brand file must not silently drop the signature."""
    monkeypatch.setattr(sending, "html_signature", lambda: "")
    campaign = FakeCampaign({"signature": "Yinusa Yakubu Temitope"})
    body, is_html = sending.message_body(FakeDraft("The email."), campaign)
    assert not is_html
    assert "Yinusa Yakubu Temitope" in body


def test_the_explanatory_comment_is_not_mailed(tmp_path):
    from app.html_mail import load_signature_html

    file = tmp_path / "sig.html"
    file.write_text("<!-- paste this into Zoho -->\n<table>real</table>",
                    encoding="utf-8")
    assert load_signature_html(file).startswith("<table>")
