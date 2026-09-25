"""Telling apart the three things that arrive after you write to someone.

A follow-up sent to a fund that already replied undoes the one thing that
went right, so the signal that stops it has to be trustworthy before
follow-ups exist at all.

Three kinds of inbound, wrong in three different ways if confused:

    a reply is a person, and stops everything;
    an automatic reply is a machine saying nobody is reading, and must
    not be mistaken for interest;
    a bounce means the address is wrong and nothing arrived, so the
    target has not been contacted at all.

The classifiers are what these tests pin. Matching against the live
mailbox was checked separately: a real inbound from welcome@zoho.com was
detected, dated correctly, and not recorded twice on a second run.
"""

from __future__ import annotations

from app.replies import AUTO_REPLY, BOUNCE_SENDER, BOUNCE_SUBJECT, _sender, _text_of


def test_a_display_name_does_not_hide_the_sender():
    assert _sender({"fromAddress": '&quot;Madica&quot;&lt;hello@madica.vc&gt;'}) \
        == "hello@madica.vc"


def test_no_sender_is_not_a_crash():
    assert _sender({"fromAddress": ""}) is None
    assert _sender({}) is None


def test_subject_and_snippet_are_searched_together():
    """An out of office often says so in the body, not the subject."""
    text = _text_of({"subject": "Re: Halene",
                     "summary": "I am currently out of the office until Monday."})
    assert AUTO_REPLY.search(text)


# ------------------------------------------------------- automatic replies

def test_the_usual_ways_of_saying_i_am_away():
    for line in ("Out of Office: Re: Halene",
                 "Automatic reply: your message",
                 "Auto-Reply from the team",
                 "I am on annual leave until 3 October",
                 "Réponse automatique"):
        assert AUTO_REPLY.search(line), line


def test_a_real_reply_is_not_mistaken_for_one():
    for line in ("Re: Halene, happy to talk next week",
                 "Thanks, can you send the deck?",
                 "We are reviewing this internally",
                 "Interesting. What is your traction so far?"):
        assert not AUTO_REPLY.search(line), line


def test_an_office_mentioned_in_passing_is_not_an_auto_reply():
    """The phrase has to be somebody saying they are away."""
    assert not AUTO_REPLY.search("Our office is in Lagos, come and visit")


# ------------------------------------------------------------------ bounces

def test_the_usual_bounce_subjects():
    for line in ("Undeliverable: Halene",
                 "Delivery Status Notification (Failure)",
                 "Mail delivery failed: returning message to sender",
                 "Returned mail: see transcript for details",
                 "Message not delivered"):
        assert BOUNCE_SUBJECT.search(line), line


def test_postmasters_are_recognised_by_address():
    for address in ("mailer-daemon@fund.com", "postmaster@fund.com",
                    "MAILER-DAEMON@Fund.com"):
        assert BOUNCE_SENDER.match(address), address


def test_a_person_at_a_fund_is_not_a_postmaster():
    for address in ("hello@madica.vc", "partner@fund.com", "info@fund.africa"):
        assert not BOUNCE_SENDER.match(address), address


def test_a_reply_about_delivery_dates_is_not_a_bounce():
    """"Delivery" in a sentence is not a delivery status notification."""
    assert not BOUNCE_SUBJECT.search("Re: delivery timeline for the pilot")
