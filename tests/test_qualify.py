"""Qualification tests.

The cases that matter are the ones where the score and the right answer
disagree: a fund that scores well on everything except the one thing that
makes it impossible, and a fund that scores well and still must not be
emailed.
"""

from __future__ import annotations

from app.qualify import SenderProfile, assess, route
from app.taxonomy import canonical_sector, canonical_stage, geo_covers, stage_floor_above

SENDER = SenderProfile(
    stage="pre-seed",
    raise_target_usd=250_000,
    sectors=["energy", "embedded finance", "climate"],
    geography="nigeria",
    competitors=["Optasia"],
)

GOOD = {
    "stages": ["pre-seed", "seed"],
    "sectors": ["climate", "fintech", "energy"],
    "geographies": ["pan-African"],
    "cheque_min_usd": 200_000,
    "last_deal_at": "2026-07",
    "portfolio": [{"name": "Keep IT Cool"}],
    "accepts_cold": None,
    "application_url": None,
}


def _with(**kw):
    out = dict(GOOD)
    out.update(kw)
    return out


# ------------------------------------------------------------- the taxonomy

def test_sector_synonyms_map_to_one_canonical_term():
    for phrase in ["financial technology", "embedded finance", "payments", "lending"]:
        assert canonical_sector(phrase) == "fintech"
    for phrase in ["clean energy", "off-grid", "energy access", "solar"]:
        assert canonical_sector(phrase) == "energy"


def test_pre_seed_does_not_collapse_into_seed():
    """Longest synonym wins, or every pre-seed fund reads as seed."""
    assert canonical_stage("pre-seed") == "pre-seed"
    assert canonical_stage("Pre Seed") == "pre-seed"
    assert canonical_stage("seed") == "seed"


def test_africa_covers_nigeria_but_kenya_does_not():
    assert geo_covers("pan-African", "nigeria")
    assert geo_covers("West Africa", "nigeria")
    assert geo_covers("global", "nigeria")
    assert not geo_covers("Kenya", "nigeria")
    assert not geo_covers("Southeast Asia", "nigeria")


def test_series_a_floor_is_detected():
    assert stage_floor_above(["Series A", "Series B"], "pre-seed")
    assert not stage_floor_above(["pre-seed", "seed"], "pre-seed")


# ------------------------------------------------------------- the hard gates

def test_a_good_fund_qualifies():
    v = assess(GOOD, SENDER)
    assert v.verdict == "qualified"
    assert v.score >= 70


def test_series_a_floor_is_rejected_however_well_it_scores():
    """The case the weighting would otherwise hide."""
    v = assess(_with(stages=["Series A", "Series B"]), SENDER)
    assert v.verdict == "rejected"
    assert "later than pre-seed" in v.rejection_reason


def test_wrong_continent_is_rejected_however_well_it_scores():
    v = assess(_with(geographies=["Southeast Asia"]), SENDER)
    assert v.verdict == "rejected"
    assert "does not cover nigeria" in v.rejection_reason


def test_cheque_minimum_larger_than_the_whole_raise_scores_zero():
    v = assess(_with(cheque_min_usd=2_000_000), SENDER)
    cheque = next(c for c in v.criteria if c.name == "cheque")
    assert cheque.points == 0
    assert "exceeds the whole raise" in cheque.note


def test_a_competitor_in_the_portfolio_forces_review():
    v = assess(_with(portfolio=[{"name": "Optasia"}]), SENDER)
    assert v.verdict == "needs_review"
    assert "Optasia" in v.rejection_reason


def test_a_substring_of_a_competitor_name_is_not_a_conflict():
    """'Optasia' must not match inside 'Optasia Holdings Group' by accident
    of substring, nor 'Opt' match 'Optasia'."""
    v = assess(_with(portfolio=[{"name": "Opt"}]), SENDER)
    assert v.verdict != "needs_review" or "Optasia" not in (v.rejection_reason or "")


def test_missing_data_is_partial_credit_not_zero():
    """Silence on a website is not a no, and must not read as one."""
    sparse = {"stages": [], "sectors": [], "geographies": [], "portfolio": []}
    v = assess(sparse, SENDER)
    assert v.verdict != "rejected"
    for name in ("stage", "sector", "geography"):
        c = next(x for x in v.criteria if x.name == name)
        assert 0 < c.points < c.max_points


def test_a_long_dead_fund_loses_the_activity_points():
    v = assess(_with(last_deal_at="2019"), SENDER)
    activity = next(c for c in v.criteria if c.name == "activity")
    assert activity.points == 0


def test_every_criterion_explains_itself():
    v = assess(GOOD, SENDER)
    assert len(v.criteria) == 6
    assert all(c.note for c in v.criteria)
    assert v.as_json()["criteria"][0]["name"] == "stage"


# ---------------------------------------------------------------- the routing

def test_an_application_form_beats_a_found_address():
    """If a fund says how it wants to be approached, that is the answer."""
    channel, note = route({"application_url": "https://x.vc/apply"}, has_email=True)
    assert channel == "form"
    assert note == "https://x.vc/apply"


def test_no_cold_approaches_means_intro_not_email():
    channel, note = route({"accepts_cold": False}, has_email=True)
    assert channel == "intro"
    assert "unsolicited" in note


def test_an_address_gets_email():
    assert route({}, has_email=True)[0] == "email"


def test_no_address_and_no_form_means_intro():
    channel, note = route({}, has_email=False)
    assert channel == "intro"
    assert "no published address" in note


def test_routing_ignores_the_score():
    """A perfect fund that publishes a form still must not be emailed."""
    v = assess(GOOD, SENDER)
    assert v.verdict == "qualified"
    assert route(_with(application_url="https://x.vc/apply"), has_email=True)[0] == "form"


def test_a_dormant_fund_goes_to_review_however_well_it_scores():
    """Activity is only 10 points, so a fund dead since 2019 still clears
    70 on sector and geography. That is dead capital dressed as a good
    fit, and the weighting alone would wave it through."""
    v = assess(_with(last_deal_at="2019", geographies=["Nigeria"]), SENDER)
    assert v.score >= 70
    assert v.verdict == "needs_review"
    assert "nothing since 2019" in v.rejection_reason


def test_an_undated_fund_is_not_treated_as_dormant():
    """No date on the site is not evidence of a dead fund."""
    v = assess(_with(last_deal_at=None), SENDER)
    assert v.verdict == "qualified"


def test_early_stage_is_evidence_of_fit_not_confirmation():
    """Funds very often say only 'early stage'. Treating that as unknown
    sends most of the market to review; treating it as a match overstates
    it. It scores between."""
    v = assess(_with(stages=["Early"]), SENDER)
    stage = next(c for c in v.criteria if c.name == "stage")
    assert stage.max_points // 3 < stage.points < stage.max_points
    assert "may include pre-seed" in stage.note


def test_a_stated_stage_still_beats_an_umbrella_term():
    v = assess(_with(stages=["pre-seed", "early stage"]), SENDER)
    stage = next(c for c in v.criteria if c.name == "stage")
    assert stage.points == stage.max_points
