"""The phrases that make a cold email read like every other cold email.

The list earns its keep in two directions. It has to catch the warm
noises a model reaches for when it has nothing specific to say, and it
has to stay silent on words that are only a cliche in one sense. An
energy company writes about disrupted supply and unlocked meters in the
ordinary course of business, and a checker that flags those teaches the
operator to ignore it.
"""

from __future__ import annotations

from app.house_style import find, for_prompt


def test_the_opener_that_prompted_this():
    assert "resonate" in find("At Connected Intelligence, we resonate "
                              "deeply with this mission.")


def test_deeply_is_caught_on_its_own():
    assert "deeply" in find("We care deeply about metering.")


def test_the_subject_line_that_prompted_this():
    assert find("Unlocking Energy Access in Nigeria")


def test_aligning_with_their_mission():
    """Reported as written, so a rewrite can be told what to change.

    Telling a model it used "reaching out" when it wrote "reached out"
    is a complaint it cannot act on, and it wrote "reached out" again.
    """
    assert find("Connected Intelligence is aligning with your mission "
                "as we build Halene.") == ["aligning with your"]


def test_every_inflection_of_a_phrase_is_caught():
    """A plain string caught "reaching out" and let "reached out" through,
    which is exactly what a follow-up reaches for."""
    assert find("I reached out last week") == ["reached out"]
    assert find("reaching out again") == ["reaching out"]
    assert find("we circled back") == ["circled back"]


def test_a_non_word_match_keeps_its_name():
    """There is nothing readable to quote back for punctuation."""
    assert find("We build Halene — a facility") == ["an em dash"]


def test_stock_openers_still_caught():
    for phrase in ("reaching out", "circling back", "excited to",
                   "would love to", "thrilled"):
        assert find(f"I am {phrase} about this."), phrase


def test_em_dash_is_caught():
    """The writing rule that predates this module."""
    assert "an em dash" in find("We build Halene \u2014 an emergency facility.")


def test_disrupted_supply_is_literal():
    """A real sentence from a real draft. Banning this makes writing worse."""
    assert find("The forced halt of airtime credit disrupted services "
                "for 40 million people.") == []


def test_disrupting_a_market_is_not():
    assert find("We are disrupting the energy market.")


def test_a_meter_can_be_unlocked():
    assert find("Customers unlock the meter with a token.") == []


def test_unlocking_something_abstract_cannot():
    assert find("unlocking the potential of prepaid metering")


def test_subject_and_body_are_searched_together():
    assert find("Seamless energy", "A plain body.")


def test_what_was_written_is_what_is_reported():
    """The stem is for matching; a person should see the actual word."""
    assert find("This is groundbreaking and innovative.") == [
        "groundbreaking", "innovative"]


def test_prompt_shows_words_not_stems():
    rendered = for_prompt()
    assert '"resonate"' in rendered and '"resonat"' not in rendered
    assert '"innovative"' in rendered and '"innovativ";' not in rendered


def test_a_plain_sentence_is_left_alone():
    assert find("We fund the advance and a licensed lender is lender of "
                "record. Pre-seed, raising $250,000.") == []


# ------------------------------------------- the follow-up vocabulary

def test_the_phrases_a_second_email_reaches_for():
    for line in ("I reached out last week", "reaching out again",
                 "we circled back", "just following up",
                 "touching base", "bumping this to the top",
                 "per my last email", "a gentle reminder",
                 "in case you missed it"):
        assert find(line), line


def test_check_in_is_only_a_cliche_without_an_object():
    """"We check in customers at the meter" is a real sentence here.

    The cliche never takes an object: "I wanted to check in regarding",
    "just checking in.". That is the difference the pattern uses.
    """
    for line in ("I wanted to check in regarding the email",
                 "just checking in.", "checking in on this",
                 "checking in about the round"):
        assert find(line), line
    for line in ("we check in customers at the meter",
                 "the system checks in tokens nightly"):
        assert not find(line), line


# ------------------------------------------------ greetings and sign-offs

def test_a_greeting_is_removed():
    """The prompt says not to write one. It mostly obeys."""
    from app.house_style import strip_edges

    assert strip_edges("Hi,\n\nThe body.") == "The body."
    assert strip_edges("Dear Madica Team,\n\nThe body.") == "The body."
    assert strip_edges("Good morning,\nThe body.") == "The body."


def test_a_sign_off_is_removed():
    from app.house_style import strip_edges

    assert strip_edges("The body.\n\nBest regards,\nYinusa") == "The body."
    assert strip_edges("The body.\n\nThanks") == "The body."


def test_a_body_with_neither_is_untouched():
    from app.house_style import strip_edges

    body = "We fund a small capped advance at the meter."
    assert strip_edges(body) == body


def test_a_word_that_merely_starts_like_a_greeting_survives():
    from app.house_style import strip_edges

    assert strip_edges("Hi-tech metering is the point.") \
        == "Hi-tech metering is the point."
