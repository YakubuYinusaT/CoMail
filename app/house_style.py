"""Phrases that make a cold email read like every other cold email.

Two groups, and the second is the one that prompted this module.

The first is the stock opener vocabulary: "reaching out", "circling
back", "excited to". Those were already in the prompt and mostly stayed
out of the drafts.

The second is manufactured affinity and inflated register, which the
prompt did not name and which went straight through. A real draft opened

    At Connected Intelligence, we resonate deeply with this mission

and another was titled "Unlocking Energy Access in Nigeria". Nobody
resonates with a fund's mission statement, and nothing is unlocked. The
words are there to sound warm, and an investor reading forty of these a
day recognises them instantly.

One list, used twice: the prompt is rendered from it, and the draft is
checked against it afterwards. Keeping them apart would let them drift,
and the sender-claim failure already showed what a prompt rule is worth
on its own.
"""

from __future__ import annotations

import re
import textwrap

# Matched with a word boundary and any suffix, so "resonat" covers
# resonate, resonates, resonated and resonating.
STEMS = {
    # stock opener vocabulary
    "synerg", "excited to",
    "aligns perfectly", "perfect fit", "significant fit", "innovativ",
    "cutting-edge", "passionate about", "would love to",
    "i hope this finds you well",

    # manufactured affinity: claiming to feel something about them
    "resonat", "deeply", "mission-driven", "at the intersection of",
    "speaks to", "inspired by your", "admire your", "share your vision",
    "shared vision", "we believe there is",

    # inflated register: dressing a plain fact as an event
    "transformativ", "transformational", "unleash", "empower",
    "revolutionar", "revolutioni", "game-chang", "groundbreaking",
    "seamless", "holistic", "best-in-class", "world-class", "pivotal",
    "poised to", "rapidly evolving", "in today's", "landscape", "journey",
    "thrilled", "delighted", "eager to", "robust",
}

# Things a stem cannot express, and two words that are only a cliche in
# one sense. "The forced halt of airtime credit disrupted services for 40
# million people" is literal and correct; "disrupting the market" is not.
# A prepaid meter can genuinely be unlocked, so only the figurative
# objects count.
PATTERNS = {
    "disrupting the market": r"\bdisrupt\w*\s+(the\s+)?(\w+\s+)?(market|industry|sector|space|status quo)\b",
    "unlocking something abstract": r"\bunlock\w*\s+(the\s+)?(\w+\s+)?(potential|value|growth|access|opportunit\w*|future)\b",
    # Multi-word phrases inflect. A plain string caught "reaching out"
    # and let "reached out" through, which is exactly what a follow-up
    # reaches for. The verb is matched by stem, not by one spelling.
    "reaching out": r"\breach\w*\s+out\b",
    "circling back": r"\bcircl\w*\s+back\b",
    "following up": r"\b(just |simply )?follow\w*\s+up\b",
    # "we check in customers at the meter" is a real sentence in this
    # business, and "I wanted to check in" is the cliche. The difference
    # is whether it takes an object: the cliche never does.
    "checking in": r"\bcheck\w*\s+in\b(?=\s*(?:[,.!?]|$|on\b|about\b|regarding\b|with\b|to see\b|re\b))",
    "touching base": r"\btouch\w*\s+base\b",
    "bumping this": r"\bbump\w*\s+(this|it|the thread)\b",
    "in case you missed it": r"\bin case you missed it\b",
    "per my last email": r"\bper my (last|previous) (e-?mail|message)\b",
    "gentle reminder": r"\b(gentle|friendly) (reminder|nudge)\b",
    "aligning with your mission": r"\balign\w*\s+(deeply\s+)?with\s+(your|their|this|our)\b",
    "an em dash": r"\u2014",
    "leverage as a verb": r"\bleverag(e|es|ed|ing)\b",
}


def find(*texts: str) -> list[str]:
    """Every banned phrase present, as written, deduplicated and ordered."""
    hay = " \n ".join(t or "" for t in texts)
    lowered = hay.lower()
    hits: list[str] = []

    for stem in STEMS:
        pattern = (re.escape(stem) if " " in stem or "-" in stem
                   else r"\b" + re.escape(stem) + r"\w*")
        match = re.search(pattern, lowered)
        if match:
            hits.append(match.group(0).strip())

    for name, pattern in PATTERNS.items():
        match = re.search(pattern, hay, re.I)
        if not match:
            continue
        # Report what was written, not the canonical name. Telling a model
        # it used "reaching out" when it wrote "reached out" is a complaint
        # it cannot act on, and it wrote "reached out" again.
        written = match.group(0).strip().lower()
        hits.append(written if written.isascii() and written.replace(" ", "").isalpha()
                    else name)

    # "unlock" is inside "unlocking"; report what was actually written.
    return sorted({h for h in hits
                   if not any(h != other and h in other for other in hits)})

# Stems are for matching. A prompt listing "resonat" and "innovativ"
# reads as a list of typos, so they are spelled out for the model.
DISPLAY = {
    "synerg": "synergy",
    "innovativ": "innovative",
    "resonat": "resonate",
    "transformativ": "transformative",
    "revolutionar": "revolutionary",
    "revolutioni": "revolutionise",
    "game-chang": "game-changer",
}


def for_prompt(width: int = 68) -> str:
    """The list as the prompt should show it: readable, not a data dump."""
    words = sorted(DISPLAY.get(stem, stem) for stem in STEMS
                   if stem != "i hope this finds you well")
    body = "; ".join(f'"{w}"' for w in words)
    wrapped = textwrap.fill(body, width=width,
                            initial_indent="  ", subsequent_indent="  ")
    return ('  em dashes; "I hope this finds you well"; leverage as a verb;\n'
            '  aligning or resonating with their mission; and:\n\n' + wrapped)


# The prompt says to write no greeting and no sign-off, because both are
# added at send time and a model guessing at a name is a way to address
# an investor as "Dear Team". It mostly obeys and sometimes does not, so
# this removes them rather than trusting it.
GREETING = re.compile(
    r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))"
    r"\b[^\n]{0,60}?[,.!]?\s*\n+", re.I)

SIGN_OFF = re.compile(
    r"\n\s*(best|best regards|kind regards|regards|sincerely|thanks|"
    r"thank you|cheers|warmly|yours( sincerely| faithfully)?)"
    r"\s*[,.!]?\s*(?:\n.{0,80})?\s*$", re.I)


def strip_edges(body: str) -> str:
    """Remove a greeting or a sign-off the model added anyway."""
    text = (body or "").strip()
    previous = None
    while text != previous:
        previous = text
        text = GREETING.sub("", text, count=1).strip()
        text = SIGN_OFF.sub("", text, count=1).strip()
    return text
