"""What the email claims about us.

Every other guard in this project checks the draft against a page we
crawled. There is no page for Connected Intelligence, so claims about the
sender have had nothing behind them, and the first real batch showed what
that costs. The campaign profile said

    Partnership letters sent to EKEDC and Ikeja Electric. No signed
    agreements.

and both drafts turned that into

    we have initiated partnerships with major players like EKEDC and
    Ikeja Electric

The prompt already forbade exactly this, in as many words. It happened
anyway, which is the argument for a check rather than firmer wording.

This does not block or rewrite. It finds the sentences that assert
progress on our own behalf and puts them in front of a person with the
matching lines of the profile beside them, so vetting a draft means
reading two sentences rather than re-reading the whole email. That fits
how these drafts are actually approved: read, corrected, then sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words that assert a relationship, a deployment or a permission. Flagged
# wherever they appear in a sentence about us, because there is no
# innocent reading of "we signed" or "our pilot".
RELATIONSHIP = {
    "partnership", "partnerships", "partner", "partners", "partnered",
    "signed", "agreement", "agreements", "contract", "contracts", "mou",
    "pilot", "pilots", "piloting", "launched", "deployed", "deployment",
    "rollout", "integrated", "integration", "onboarded", "secured",
    "trial", "trials", "operating", "collaboration", "collaborating",
    "committed", "backed", "working with", "in production", "live",
}

# These are only a claim when they are ours or counted. "an emergency
# credit facility for prepaid electricity customers" describes the market
# and is true. "our customers" or "4,000 users" asserts traction. Without
# the split the panel flags every draft twice and stops being read.
COUNTED = {"users", "customers", "subscribers", "revenue", "traction"}
OWNED = re.compile(r"(\bour|\bwe have|\bwe now have|\d[\d,.]*\s*(k|m|million)?)\s+\w*\s*$", re.I)

# Marks a sentence as being about us rather than about them.
FIRST_PERSON = re.compile(r"\b(we|we're|we've|our|ours|us)\b", re.I)

# Hedges that show the profile is explicit about what has not happened. A
# claim whose word appears only beside one of these is the clearest
# possible miss.
NEGATION = re.compile(
    r"\b(no|not|none|never|yet to|without|pending|unsigned|awaiting)\b", re.I)

SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def stem(word: str) -> str:
    """Crude, and enough: "partnerships" and "Partnership letters" must meet."""
    return re.sub(r"(ies|es|s|ed|ing)$", "", word.split()[0])


@dataclass
class Claim:
    """One sentence about us that asserts something, and its basis."""

    sentence: str
    triggers: list[str]
    # Lines of the sender profile bearing on it, verbatim.
    basis: list[str]
    # True when the profile only ever mentions this in the negative.
    contradicted: bool

    @property
    def severity(self) -> str:
        if self.contradicted:
            return "contradicted"
        if not self.basis:
            return "unsupported"
        return "check"


def profile_units(pitch: str) -> list[str]:
    """The profile as whole statements, not as physical lines.

    The pitch is hand-wrapped, so splitting on newlines cuts sentences in
    half and separates a claim from its qualifier. "Partnership letters
    sent to EKEDC" landed on one line and "No signed agreements" on the
    next, which is precisely the pair a person needs to see together.

    Blank lines separate blocks; inside a block, lines starting with a
    dash are their own item and indented lines continue the one above.
    """
    units: list[str] = []
    for block in re.split(r"\n\s*\n", pitch or ""):
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            starts_item = raw.lstrip().startswith("-")
            continues = bool(units) and not starts_item and raw[:1].isspace()
            if continues or (units and not starts_item and not raw[:1].isspace()
                             and not units[-1].endswith((".", ":", "!", "?"))):
                units[-1] += " " + line
            else:
                units.append(line.lstrip("- ").strip())
    return units



def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.findall(text or "") if s.strip()]


def _hits(sentence: str) -> list[str]:
    lowered = sentence.lower()
    found: list[str] = []

    for word in RELATIONSHIP:
        pattern = (re.escape(word) if " " in word
                   else r"\b" + re.escape(word) + r"\b")
        if re.search(pattern, lowered):
            found.append(word)

    for word in COUNTED:
        for match in re.finditer(r"\b" + re.escape(word) + r"\b", lowered):
            if OWNED.search(lowered[:match.start()]):
                found.append(word)
                break

    # "partnership" is inside "partnerships"; keep only the word actually
    # written, so the reason shown to a person matches the sentence.
    return sorted({w for w in found
                   if not any(w != other and w in other for other in found)})


def review(body: str, pitch: str) -> list[Claim]:
    """Sentences about us that assert progress, with the profile beside them."""
    profile_lines = profile_units(pitch)
    claims: list[Claim] = []

    for sentence in _sentences(body):
        if not FIRST_PERSON.search(sentence):
            continue
        triggers = _hits(sentence)
        if not triggers:
            continue

        basis = [ln for ln in profile_lines
                 if any(re.search(r"\b" + re.escape(stem(t)) + r"\w*", ln, re.I)
                        for t in triggers)]
        contradicted = bool(basis) and all(NEGATION.search(ln) for ln in basis)
        claims.append(Claim(sentence=sentence, triggers=triggers,
                            basis=basis, contradicted=contradicted))

    # Worst first: a contradicted claim is the one that ends a meeting.
    order = {"contradicted": 0, "unsupported": 1, "check": 2}
    claims.sort(key=lambda c: order[c.severity])
    return claims
