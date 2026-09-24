"""Hook selection and the sameness gate.

Both run without an API key. Hook selection is where the "no invented
warmth" rule is enforced, and the gate is the only thing that will
notice forty emails converging on one sentence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.generate import NoHook, choose_hook, validate_used_facts, GeneratedEmail
from app.qualify import SenderProfile
from app.sameness import compare_batch, similarity

SENDER = SenderProfile(
    stage="pre-seed",
    sectors=["energy", "embedded finance", "climate"],
    geography="nigeria",
)


@dataclass
class FakeFact:
    """Stands in for a Fact row. Only the fields hook selection reads."""

    kind: str
    claim: str
    confidence: str = "verified"
    source_url: str = "https://example.vc/x"
    observed_at: datetime = None  # type: ignore[assignment]
    id: uuid.UUID = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.observed_at is None:
            self.observed_at = datetime.now(timezone.utc)
        if self.id is None:
            self.id = uuid.uuid4()


# ------------------------------------------------------------ hook selection

def test_portfolio_in_our_sector_wins_as_the_opening():
    facts = [
        FakeFact("portfolio", "Backed Kola Energy, a Lagos off-grid metering company."),
        FakeFact("thesis", "States it invests in African founders."),
    ]
    hook = choose_hook(facts, {"thesis_quote": "invests in African founders"}, SENDER)
    assert hook.kind == "portfolio_adjacent"
    assert "energy" in hook.rationale


def test_an_unrelated_portfolio_company_does_not_count_as_adjacent():
    """'You backed a company' is true and worthless. It must fall through
    to a hook that actually says something."""
    facts = [
        FakeFact("portfolio", "Backed Foldy, a stationery marketplace."),
        FakeFact("thesis", "States it invests in African founders."),
    ]
    hook = choose_hook(facts, {"thesis_quote": "invests in African founders"}, SENDER)
    assert hook.kind == "thesis_quote"


def test_thesis_hook_needs_both_a_quote_and_a_fact():
    """The quote is what gets put in quotation marks, so it has to exist."""
    facts = [FakeFact("thesis", "States it backs infrastructure.")]

    # A thesis fact with no verbatim quote to go with it cannot open an
    # email, because the opening would be a paraphrase in quotation marks.
    assert isinstance(choose_hook(facts, {}, SENDER), NoHook)

    # With the quote present, the same fact carries the hook.
    hook = choose_hook(facts, {"thesis_quote": "backs infrastructure"}, SENDER)
    assert hook.kind == "thesis_quote"


def test_two_deals_make_a_cluster():
    facts = [
        FakeFact("deal", "Invested in Solstice in March 2026."),
        FakeFact("deal", "Invested in Gridline in May 2026."),
    ]
    hook = choose_hook(facts, {}, SENDER)
    assert hook.kind == "recent_cluster"
    assert len(hook.facts) == 2


def test_a_single_deal_becomes_a_fund_event_only_if_it_reads_like_one():
    event = [FakeFact("deal", "Announced a $30m second close in July 2026.")]
    assert choose_hook(event, {}, SENDER).kind == "fund_event"

    plain = [FakeFact("deal", "Invested in Solstice.")]
    assert isinstance(choose_hook(plain, {}, SENDER), NoHook)


def test_publication_is_used_when_nothing_stronger_exists():
    facts = [FakeFact("publication", "Wrote about metering gaps in West Africa.")]
    assert choose_hook(facts, {}, SENDER).kind == "publication"


def test_no_facts_means_no_draft():
    """The rule the whole design rests on: nothing true to say means say
    nothing, not something warm and invented."""
    hook = choose_hook([], {}, SENDER)
    assert isinstance(hook, NoHook)
    assert "no verified facts" in hook.reason


def test_inferred_facts_cannot_carry_an_opening():
    """Inferred facts inform scoring. They are never quoted."""
    facts = [
        FakeFact("portfolio", "Probably backs energy companies.", confidence="inferred"),
        FakeFact("thesis", "Seems to like infrastructure.", confidence="inferred"),
    ]
    hook = choose_hook(facts, {"thesis_quote": "likes infrastructure"}, SENDER)
    assert isinstance(hook, NoHook)


def test_invented_fact_ids_are_discarded():
    real = [FakeFact("portfolio", "Backed Kola Energy.")]
    email = GeneratedEmail(
        subject="x", body="y",
        used_fact_ids=[str(real[0].id), str(uuid.uuid4())],
    )
    kept = validate_used_facts(email, real)
    assert len(kept) == 1
    assert kept[0] is real[0]


# --------------------------------------------------------------- the gate

MAIL_A = (
    "You backed Kola Energy, which put prepaid metering into Lagos homes. "
    "We are building the credit layer that sits on those meters, so a "
    "customer who runs out at midnight stays on supply and repays on their "
    "next purchase. Nigeria crossed 7.74 million metered customers in June. "
    "Twenty minutes to see if this fits?"
)
MAIL_B_NEAR_COPY = (
    "You backed Gridline, which put prepaid metering into Accra homes. "
    "We are building the credit layer that sits on those meters, so a "
    "customer who runs out at midnight stays on supply and repays on their "
    "next purchase. Nigeria crossed 7.74 million metered customers in June. "
    "Twenty minutes to see if this fits?"
)
MAIL_C_DIFFERENT = (
    "Your second close in July put $30m behind climate adaptation. Prepaid "
    "electricity is where adaptation meets a payment problem: 4.85 million "
    "Nigerian customers are still unmetered, and the ones who are metered "
    "lose supply at the worst possible hour. We fund the gap and recover it "
    "on the next token purchase. Is this the kind of thing the new fund "
    "looks at?"
)


def test_a_near_copy_is_caught():
    """The actual failure mode: same letter, names swapped."""
    assert similarity(MAIL_A, MAIL_B_NEAR_COPY) > 0.72


def test_a_genuinely_different_email_is_not_flagged():
    assert similarity(MAIL_A, MAIL_C_DIFFERENT) < 0.72


def test_batch_flags_only_the_offenders():
    report = compare_batch(
        {"a": MAIL_A, "b": MAIL_B_NEAR_COPY, "c": MAIL_C_DIFFERENT}, threshold=0.72
    )
    assert set(report.flagged) == {"a", "b"}
    assert report.worst["c"] < 0.72


def test_worst_pair_is_reported_for_review():
    report = compare_batch({"a": MAIL_A, "b": MAIL_B_NEAR_COPY, "c": MAIL_C_DIFFERENT})
    pair = report.worst_pair
    assert {pair.left, pair.right} == {"a", "b"}


def test_identical_text_scores_one():
    assert similarity(MAIL_A, MAIL_A) == 1.0


def test_punctuation_and_case_do_not_hide_a_copy():
    assert similarity("Hello there, investor!", "hello there investor") > 0.9


def test_a_single_draft_is_never_flagged():
    report = compare_batch({"only": MAIL_A})
    assert report.flagged == []
    assert report.worst["only"] == 0.0
