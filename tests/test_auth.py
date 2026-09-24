"""The shared password and its cookie.

Signing is stdlib HMAC, so every case here is exact rather than
probabilistic. The ones that matter are the forgeries: a cookie whose
signature was not produced by our key must never open the queue.
"""

from __future__ import annotations

import time

import pytest

from app import auth
from app.config import settings
from app.web import _safe_next


@pytest.fixture(autouse=True)
def password(monkeypatch):
    """Turn login on for the duration of a test.

    settings() is lru_cached, so the object is patched in place and the
    cache cleared after, or the next test inherits a password.
    """
    s = settings()
    monkeypatch.setattr(s, "app_password", "correct horse", raising=False)
    monkeypatch.setattr(s, "session_secret", "", raising=False)
    monkeypatch.setattr(s, "session_max_age_days", 14, raising=False)
    yield s


def test_enabled_follows_the_password(monkeypatch, password):
    assert auth.enabled()
    monkeypatch.setattr(password, "app_password", "")
    assert not auth.enabled()


def test_round_trip():
    assert auth.valid(auth.issue())


def test_rejects_rubbish():
    for bad in [None, "", "nodot", "....", "!!!.!!!", "a.b"]:
        assert not auth.valid(bad)


def test_rejects_a_tampered_payload():
    cookie = auth.issue()
    body, sig = cookie.rsplit(".", 1)
    # Same signature, different payload.
    assert not auth.valid(f"{body}x.{sig}")


def test_rejects_a_signature_from_another_key(monkeypatch, password):
    cookie = auth.issue()
    monkeypatch.setattr(password, "app_password", "a different password")
    assert not auth.valid(cookie), "changing the password must sign everyone out"


def test_expires(monkeypatch):
    cookie = auth.issue()
    fifteen_days = time.time() + 15 * 86400
    monkeypatch.setattr(auth.time, "time", lambda: fifteen_days)
    assert not auth.valid(cookie)


def test_password_check():
    assert auth.check_password("correct horse")
    assert not auth.check_password("correct hors")
    assert not auth.check_password("")


def test_lockout_after_repeated_failures():
    ip = "203.0.113.9"
    auth.clear_failures(ip)
    for _ in range(auth._LOCKOUT_AFTER - 1):
        auth.note_failure(ip)
    assert auth.locked_out(ip) == 0
    auth.note_failure(ip)
    assert auth.locked_out(ip) > 0
    auth.clear_failures(ip)
    assert auth.locked_out(ip) == 0


def test_next_never_leaves_the_app():
    assert _safe_next("/draft/abc") == "/draft/abc"
    assert _safe_next("/targets?x=1") == "/targets?x=1"
    # Protocol-relative and absolute URLs are the open-redirect cases.
    assert _safe_next("//evil.example") == "/"
    assert _safe_next("https://evil.example") == "/"
    assert _safe_next("") == "/"
