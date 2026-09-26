"""When a second email is owed, and the many times it is not.

Generating the text is the easy half. Deciding that a fund should be
nudged is where the damage would be, because every wrong yes is a real
email to a real investor saying the sender was not paying attention.

So these test the refusals one at a time. Each reason is named rather
than being a bare False, since "why did Cohandle not follow up on this
one" is a question somebody will ask of a row on a screen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.followup import verdict

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def check(**overrides) -> str | None:
    """A target that is owed a follow-up, unless a test says otherwise."""
    args = dict(
        send_state="sent", sent_at=NOW - timedelta(days=10),
        draft_status="approved", step=1, answered=False,
        already_written=False, now=NOW, after_days=7, max_step=2,
    )
    args.update(overrides)
    return verdict(**args)


def test_the_case_a_follow_up_exists_for():
    """Sent, ten days ago, no answer."""
    assert check() is None


# ------------------------------------------------------- it did not go

def test_a_draft_still_sitting_in_zoho_is_not_a_sent_email():
    """The whole point of reconciling the Sent folder."""
    why = check(send_state="drafted")
    assert why and "not sent" in why


def test_a_bounce_stops_it():
    """Nothing arrived, and a second copy will not arrive either."""
    why = check(send_state="bounced")
    assert why and "not sent" in why


def test_no_send_date_means_the_wait_cannot_be_measured():
    assert check(sent_at=None) is not None


# --------------------------------------------------------- they answered

def test_a_reply_stops_it():
    """The worst thing this tool could do is nudge somebody who replied."""
    assert check(answered=True) == "they replied"


def test_an_out_of_office_does_not_stop_it():
    """A machine saying somebody is away is not an answer.

    replies.answered() excludes automatic replies, so answered is False
    here, and the nudge is still owed once they are back.
    """
    assert check(answered=False) is None


# ------------------------------------------------------------ too soon

def test_too_soon():
    why = check(sent_at=NOW - timedelta(days=2))
    assert why and "2d since sending" in why


def test_the_day_it_becomes_due():
    assert check(sent_at=NOW - timedelta(days=7)) is None


def test_the_day_before():
    assert check(sent_at=NOW - timedelta(days=6, hours=23)) is not None


def test_the_wait_is_configurable():
    assert check(sent_at=NOW - timedelta(days=4), after_days=3) is None


# ----------------------------------------------------------- decisions

def test_a_rejected_draft_stops_the_sequence():
    """Rejecting an email is a decision about the fund, not about one draft."""
    why = check(draft_status="rejected")
    assert why and "rejected" in why


def test_the_step_limit_holds():
    """Three emails to somebody silent is where persistence turns into
    a nuisance."""
    why = check(step=2, max_step=2)
    assert why and "limit" in why


def test_a_longer_sequence_can_be_configured():
    assert check(step=2, max_step=3) is None


def test_it_is_not_written_twice():
    why = check(already_written=True)
    assert why and "already been written" in why


# ------------------------------------------------------------- ordering

@pytest.mark.parametrize("state", ["drafted", "bounced", "failed", "cancelled"])
def test_anything_but_sent_is_refused_before_anything_else_is_considered(state):
    """A bounced send that is also old and unanswered is still a bounce."""
    why = check(send_state=state, sent_at=NOW - timedelta(days=90))
    assert why and "not sent" in why


def test_a_reply_outranks_the_waiting_period():
    """Answered on day one still stops it, not "too soon" then later yes."""
    assert check(answered=True, sent_at=NOW - timedelta(days=1)) == "they replied"
