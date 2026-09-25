"""Stage derivation for the intake board.

The stage is not stored, it is worked out from what exists: pages, facts,
an address, a draft, and the newest job for that target. These tests pin
the orderings that matter, particularly the ones where a wrong answer
would mislead rather than merely look odd — a target that cannot be
drafted must never offer a Draft button.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.intake import STAGE_LABEL, STAGE_ORDER, _stage, counts


@dataclass
class FakeTarget:
    enriched_at: datetime | None = None
    channel_note: str | None = None
    # Set during qualification. "form" and "intro" mean the fund
    # publishes a way in, so an address is optional rather than missing.
    channel: str | None = None
    profile: dict | None = None


@dataclass
class FakeJob:
    state: str
    attempts: int = 0
    error: str | None = None


NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
READ = FakeTarget(enriched_at=NOW)
UNREAD = FakeTarget()


def stage(target=UNREAD, pages=0, facts=0, has_email=False,
          draft=None, enrich=None, generate=None, reply=None):
    return _stage(target, pages, facts, has_email, draft, enrich, generate, reply)


def test_every_stage_has_a_label_and_a_place_in_the_order():
    assert set(STAGE_LABEL) == set(STAGE_ORDER)


def test_fresh_target_waiting_on_the_queue():
    assert stage(enrich=FakeJob("pending"))[0] == "queued"


def test_running_enrich_reports_pages_so_far():
    got, detail = stage(pages=3, enrich=FakeJob("running"))
    assert got == "working"
    assert "3 pages" in detail


def test_pending_after_an_error_says_it_is_retrying():
    got, detail = stage(enrich=FakeJob("pending", attempts=1, error="timed out"))
    assert got == "queued"
    assert "timed out" in detail


def test_failed_enrich_surfaces_the_reason():
    got, detail = stage(enrich=FakeJob("failed", error="robots.txt forbids it"))
    assert got == "failed"
    assert detail == "robots.txt forbids it"


def test_read_with_no_address_and_no_published_route_waits_on_a_person():
    target = FakeTarget(enriched_at=NOW, channel_note="no route published")
    got, detail = stage(target=target, pages=4, facts=9)
    assert got == "needs_email"
    assert detail == "no route published"


def test_a_fund_with_a_form_is_not_waiting_on_an_address():
    """The form is the route. Demanding an email made four targets look
    blocked when none of them were."""
    target = FakeTarget(enriched_at=NOW, channel="form",
                        channel_note="submit through the portal")
    got, detail = stage(target=target, pages=6, facts=13)
    assert got == "apply"
    assert detail == "submit through the portal"


def test_an_address_still_unlocks_drafting_for_a_form_target():
    """You may want to ask something before filling the form in."""
    target = FakeTarget(enriched_at=NOW, channel="form")
    assert stage(target=target, pages=6, facts=13, has_email=True)[0] == "ready"


def test_an_unqualified_target_falls_back_to_what_reading_found():
    """channel_note is set during qualification, so it is empty here."""
    target = FakeTarget(enriched_at=NOW,
                        profile={"application": {"how_to_apply": "apply any time"}})
    got, detail = stage(target=target, pages=4, facts=2)
    assert got == "needs_email"
    assert detail == "apply any time"


def test_read_with_an_address_is_ready():
    assert stage(target=READ, pages=4, facts=9, has_email=True)[0] == "ready"


def test_ready_with_no_facts_says_so():
    got, detail = stage(target=READ, pages=4, facts=0, has_email=True)
    assert got == "ready"
    assert "quotable" in detail


def test_a_draft_outranks_everything_behind_it():
    assert stage(target=READ, has_email=True, draft=object(),
                 generate=FakeJob("done"))[0] == "drafted"


def test_generation_in_flight_beats_an_old_draft_state():
    assert stage(target=READ, has_email=True, generate=FakeJob("running"))[0] == "drafting"


def test_pages_but_never_read_is_stalled_not_ready():
    got, detail = stage(pages=5)
    assert got == "stalled"
    assert "never read" in detail


def test_counts_group_the_stages_a_person_cares_about():
    rows = [{"stage": s} for s in
            ("queued", "working", "drafting", "ready", "failed", "drafted",
             "apply", "intro", "replied")]
    assert counts(rows) == {"busy": 3, "ready": 1, "failed": 1,
                            "apply": 2, "replied": 1}


def test_a_reply_outranks_every_other_stage():
    """It is the only one meaning somebody is waiting on you."""
    assert STAGE_ORDER["replied"] == min(STAGE_ORDER.values())


def test_a_warm_intro_target_is_not_told_to_use_a_form():
    """Routing sends a target here when there is neither form nor address,
    so calling it "apply through their form" would invent one."""
    target = FakeTarget(enriched_at=NOW, channel="intro",
                        channel_note="no published address and no application form")
    got, detail = stage(target=target, pages=4, facts=6)
    assert got == "intro"
    assert "form" not in STAGE_LABEL["intro"]


def test_a_found_address_does_not_count_as_a_confirmed_one():
    """An address printed on a fund's website is a lead, not a decision.

    has_email drives the stage, so counting an unconfirmed finding would
    move a target along as though somebody had checked it.
    """
    target = FakeTarget(enriched_at=NOW, channel="form")
    assert stage(target=target, pages=6, facts=9, has_email=False)[0] == "apply"
    assert stage(target=target, pages=6, facts=9, has_email=True)[0] == "ready"


@dataclass
class FakeReply:
    from_email: str = "partner@fund.com"
    received_at: datetime | None = NOW


def test_a_reply_beats_even_a_finished_draft():
    """Somebody answered. Nothing else on the row matters more."""
    got, detail = stage(target=READ, pages=6, facts=9, has_email=True,
                        draft=object(), reply=FakeReply())
    assert got == "replied"
    assert "partner@fund.com" in detail
