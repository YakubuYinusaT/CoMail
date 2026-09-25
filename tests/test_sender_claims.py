"""Claims the draft makes about us.

The case that started this is the first test: the campaign profile said
letters were sent and no agreement was signed, and the model wrote that
partnerships had been initiated. Every other guard in the project checks
the draft against a crawled page, and there is no page for us, so this is
the only thing standing between that sentence and an investor.

The second thing these pin down is silence. A panel that fires on every
draft gets ignored, so describing the market must not read as a claim.
"""

from __future__ import annotations

from app.sender_claims import profile_units, review

PROFILE = """\
Connected Intelligence builds Halene, an emergency credit facility for
prepaid electricity customers in Nigeria.

Stage and traction, stated precisely:
- Pre-seed, raising $250,000.
- Team of five, based in Lagos.
- Partnership letters sent to EKEDC and Ikeja Electric, and technical
  letters to MOJEC and Conlog. No signed agreement yet.
"""


def only_with(body: str, profile: str):
    found = review(body, profile)
    assert len(found) == 1, f"expected one claim, got {[c.sentence for c in found]}"
    return found[0]


def only(body: str):
    return only_with(body, PROFILE)


def test_the_sentence_that_caused_all_this():
    claim = only("While we have initiated partnerships with major players "
                 "like EKEDC and Ikeja Electric, we seek investment.")
    assert claim.severity == "contradicted"
    # The person reading it must see the qualifier, not just the claim.
    assert "No signed agreement yet" in " ".join(claim.basis)


def test_the_softer_version_of_the_same_lie():
    claim = only("We're pre-seed, raising $250,000, with foundational "
                 "partnerships in progress.")
    assert claim.severity == "contradicted"


def test_a_claim_the_profile_never_mentions_is_unsupported():
    claim = only("We have a pilot running with two distribution companies.")
    assert claim.severity == "unsupported"
    assert claim.basis == []


def test_describing_the_market_is_not_a_claim():
    """The reason COUNTED exists. This sentence is true and must stay quiet."""
    assert review("We are building an emergency credit facility for prepaid "
                  "electricity customers in Nigeria.", PROFILE) == []


def test_counted_traction_is_a_claim():
    claim = only("We have 4,000 users on the platform today.")
    assert "users" in claim.triggers


def test_claims_about_them_are_not_ours_to_check():
    """Recipient claims have the fact guard; this must not double up."""
    assert review("Madica has partnered with founders across Africa and "
                  "signed twelve deals.", PROFILE) == []


def test_worst_first():
    body = ("We are working with a licensed lender. "
            "We have initiated partnerships with EKEDC.")
    assert [c.severity for c in review(body, PROFILE)][0] == "contradicted"


def test_profile_wrapping_does_not_split_a_statement():
    """Line-splitting separated the claim from 'No signed agreement yet'."""
    units = profile_units(PROFILE)
    joined = [u for u in units if "letters sent" in u]
    assert joined, "the bullet was lost"
    assert "No signed agreement yet" in joined[0]


def test_an_empty_body_says_nothing():
    assert review("", PROFILE) == []


def test_a_missing_profile_makes_every_claim_unsupported():
    """No profile is not permission. It is the absence of a basis."""
    claim = only_with("We signed a deal.", "")
    assert claim.severity == "unsupported"
