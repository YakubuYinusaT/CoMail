"""Evidence ranking, prompt assembly and the sameness gate.

All three run without an API key.

Ranking offers every usable opening and lets the model choose; the tests
here check that it offers rather than decides, and that the prompt
carries the one rule that is not negotiable — no specific claim about a
recipient without a fact behind it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.generate import GeneratedEmail, build_prompt, gather_evidence, validate_used_facts
from app.qualify import SenderProfile
from app.sameness import compare_batch, similarity

SENDER = SenderProfile(
    stage="pre-seed",
    sectors=["energy", "embedded finance", "climate"],
    geography="nigeria",
)


@dataclass
class FakeFact:
    """Stands in for a Fact row. Only the fields ranking reads."""

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


# --------------------------------------------------------- evidence ranking

def test_portfolio_in_our_sector_ranks_first():
    facts = [
        FakeFact("portfolio", "Backed Kola Energy, a Lagos off-grid metering company."),
        FakeFact("thesis", "States it invests in African founders."),
    ]
    ev = gather_evidence(facts, {"thesis_quote": "invests in African founders"}, SENDER)
    assert ev.kind == "portfolio_adjacent"
    assert "energy" in ev.best.rationale


def test_an_unrelated_portfolio_company_is_not_offered_as_adjacent():
    """'You backed a company' is true and worthless."""
    facts = [
        FakeFact("portfolio", "Backed Foldy, a stationery marketplace."),
        FakeFact("thesis", "States it invests in African founders."),
    ]
    ev = gather_evidence(facts, {"thesis_quote": "invests in African founders"}, SENDER)
    assert ev.kind == "thesis_quote"
    assert "portfolio_adjacent" not in [h.kind for h in ev.hooks]


def test_every_usable_opening_is_offered_not_just_the_best():
    """The model chooses. Handing it one option is the hardcoding we do
    not want."""
    facts = [
        FakeFact("portfolio", "Backed Kola Energy, an off-grid metering company."),
        FakeFact("thesis", "States it backs energy access."),
        FakeFact("deal", "Invested in Solstice."),
        FakeFact("deal", "Announced a $30m second close in July 2026."),
        FakeFact("publication", "Wrote about metering gaps in West Africa."),
    ]
    ev = gather_evidence(facts, {"thesis_quote": "backs energy access"}, SENDER)
    kinds = [h.kind for h in ev.hooks]
    assert "portfolio_adjacent" in kinds
    assert "thesis_quote" in kinds
    assert "recent_cluster" in kinds
    assert "publication" in kinds
    assert "fund_event" in kinds
    assert kinds[0] == "portfolio_adjacent"


def test_no_facts_still_produces_a_draftable_state():
    """A thin website is not a refusal. The email goes, without claims."""
    ev = gather_evidence([], {}, SENDER)
    assert ev.hooks == []
    assert ev.kind == "none"
    assert ev.quotable == []


def test_inferred_facts_are_not_quotable_and_carry_no_opening():
    facts = [
        FakeFact("portfolio", "Probably backs energy companies.", confidence="inferred"),
        FakeFact("thesis", "Seems to like infrastructure.", confidence="inferred"),
    ]
    ev = gather_evidence(facts, {"thesis_quote": "likes infrastructure"}, SENDER)
    assert ev.hooks == []
    assert ev.quotable == []


def test_prompt_without_facts_forbids_specifics_explicitly():
    """The instruction has to be in the prompt, not just in our heads."""

    class T:
        name, domain, profile = "Thin Fund", "thin.vc", {}

    class C:
        sender_profile = {}

    prompt = build_prompt(T(), gather_evidence([], {}, SENDER), SENDER, C())
    assert "NO VERIFIED FACTS" in prompt
    assert "No portfolio names" in prompt


def test_prompt_with_facts_lists_ids_and_sources():
    facts = [FakeFact("portfolio", "Backed Kola Energy, a metering company.")]

    class T:
        name, domain = "Good Fund", "good.vc"
        profile = {}

    class C:
        sender_profile = {}

    ev = gather_evidence(facts, {}, SENDER)
    prompt = build_prompt(T(), ev, SENDER, C())
    assert str(facts[0].id) in prompt
    assert facts[0].source_url in prompt
    assert "suggestions, strongest first" in prompt


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
