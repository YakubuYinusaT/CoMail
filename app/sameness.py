"""The anti-sameness gate.

A language model asked to write forty investor emails converges on its
favourite sentence. It will not notice, and neither will a reviewer
reading them one at a time. This measures it.

Character trigram Jaccard: cheap, deterministic, no model call, and it
catches the failure that actually happens — the same email with the
names swapped. Semantic embeddings would catch paraphrase too, but
paraphrase is not the problem here. Repetition is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TRIGRAM = 3


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Punctuation goes because two drafts differing only in comma
    placement are the same email.
    """
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def trigrams(text: str) -> set[str]:
    clean = normalise(text)
    if len(clean) < TRIGRAM:
        return {clean} if clean else set()
    return {clean[i : i + TRIGRAM] for i in range(len(clean) - TRIGRAM + 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of character trigrams, 0.0 to 1.0."""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union if union else 0.0


@dataclass
class Pair:
    left: str
    right: str
    score: float


@dataclass
class BatchReport:
    """Per-item worst score, plus the pairs that caused it."""

    worst: dict[str, float]
    pairs: list[Pair]
    threshold: float

    @property
    def flagged(self) -> list[str]:
        return sorted(k for k, v in self.worst.items() if v > self.threshold)

    @property
    def worst_pair(self) -> Pair | None:
        return max(self.pairs, key=lambda p: p.score) if self.pairs else None


def compare_batch(bodies: dict[str, str], threshold: float = 0.72) -> BatchReport:
    """Every draft against every other.

    Quadratic, which is fine: a batch is tens of drafts, not thousands.
    If a batch ever reaches the size where this matters, the sending caps
    have already been broken.
    """
    keys = list(bodies)
    worst = {k: 0.0 for k in keys}
    pairs: list[Pair] = []

    cache = {k: trigrams(bodies[k]) for k in keys}
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            ta, tb = cache[left], cache[right]
            union = len(ta | tb)
            score = (len(ta & tb) / union) if union else 0.0
            pairs.append(Pair(left, right, score))
            worst[left] = max(worst[left], score)
            worst[right] = max(worst[right], score)

    return BatchReport(worst=worst, pairs=pairs, threshold=threshold)
