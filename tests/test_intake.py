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


@dataclass
class FakeJob:
    state: str
    attempts: int = 0
    error: str | None = None


NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
READ = FakeTarget(enriched_at=NOW)
UNREAD = FakeTarget()


def stage(target=UNREAD, pages=0, facts=0, has_email=False,
          draft=None, enrich=None, generate=None):
    return _stage(target, pages, facts, has_email, draft, enrich, generate)


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


def test_read_but_no_address_waits_on_a_person():
    target = FakeTarget(enriched_at=NOW, channel_note="apply through the form")
    got, detail = stage(target=target, pages=4, facts=9)
    assert got == "needs_email"
    assert detail == "apply through the form"


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
            ("queued", "working", "drafting", "ready", "failed", "drafted")]
    assert counts(rows) == {"busy": 3, "ready": 1, "failed": 1}
