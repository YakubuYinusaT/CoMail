"""Vocabulary normalisation.

Funds describe themselves in their own words. One says "financial
technology", another "embedded finance", a third "payments". One invests
in "Africa", another in "Sub-Saharan Africa", another in "Lagos". Exact
string matching would reject almost everything, so qualification runs on
canonical terms and this module is what maps onto them.

This is the part that decides whether the scoring means anything, so it
is kept as plain data rather than cleverness: the lists can be read,
argued with, and extended by whoever is running a campaign.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------- stages

STAGE_ORDER = ["pre-seed", "seed", "series-a", "series-b", "growth"]

STAGE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "pre-seed": ("pre seed", "preseed", "pre-seed", "first cheque", "first check",
                 "first money in", "friends and family", "idea stage", "formation"),
    "seed": ("seed",),
    "series-a": ("series a", "seriesa", "series-a", "a round"),
    "series-b": ("series b", "series-b", "b round"),
    "growth": ("growth", "series c", "late stage", "expansion"),
}

# -------------------------------------------------------------------- sectors

SECTOR_SYNONYMS: dict[str, tuple[str, ...]] = {
    "fintech": ("fintech", "fin tech", "financial technology", "financial services",
                "payments", "embedded finance", "lending", "credit", "banking",
                "insurtech", "insurance", "wealth", "remittance"),
    "energy": ("energy", "clean energy", "renewable", "renewables", "solar",
               "energy access", "power", "electricity", "off-grid", "offgrid",
               "utilities", "grid"),
    "climate": ("climate", "climate tech", "climatetech", "climate adaptation",
                "climate resilience", "decarbonisation", "decarbonization",
                "carbon", "sustainability", "net zero"),
    "infrastructure": ("infrastructure", "infra", "digital infrastructure",
                       "hardware", "iot", "internet of things", "connectivity",
                       "telecom", "telecoms"),
    "agritech": ("agritech", "agtech", "agriculture", "agri", "farming", "food systems"),
    "healthtech": ("healthtech", "health tech", "healthcare", "health", "medtech", "biotech"),
    "logistics": ("logistics", "supply chain", "mobility", "transport", "delivery"),
    "saas": ("saas", "b2b software", "enterprise software", "software"),
    "ai": ("ai", "artificial intelligence", "machine learning", "ml", "genai"),
    "ecommerce": ("ecommerce", "e-commerce", "commerce", "retail", "marketplace"),
    "edtech": ("edtech", "education", "learning"),
    "proptech": ("proptech", "real estate", "property", "housing"),
    "deeptech": ("deeptech", "deep tech", "frontier tech", "robotics"),
}

# ------------------------------------------------------------------ geography

# What each term covers. A fund saying the key invests in everything in
# the value set, so a sender in any of them is in scope.
AFRICA = {
    "nigeria", "ghana", "kenya", "south africa", "egypt", "senegal", "ivory coast",
    "cote d'ivoire", "tanzania", "uganda", "rwanda", "ethiopia", "morocco",
    "tunisia", "algeria", "cameroon", "zambia", "zimbabwe", "botswana", "benin",
    "togo", "mali", "burkina faso", "niger", "sierra leone", "liberia", "gambia",
    "mozambique", "angola", "malawi", "namibia", "mauritius", "somalia", "sudan",
}
WEST_AFRICA = {
    "nigeria", "ghana", "senegal", "ivory coast", "cote d'ivoire", "benin", "togo",
    "mali", "burkina faso", "niger", "sierra leone", "liberia", "gambia",
}

GEO_COVERS: dict[str, set[str]] = {
    "global": AFRICA | {"global"},
    "emerging markets": AFRICA | {"emerging markets"},
    "africa": AFRICA | {"africa"},
    "pan-african": AFRICA | {"africa"},
    "sub-saharan africa": AFRICA - {"morocco", "tunisia", "algeria", "egypt"},
    "west africa": WEST_AFRICA | {"west africa"},
    "anglophone africa": {"nigeria", "ghana", "kenya", "south africa", "uganda", "zambia"},
}

GEO_SYNONYMS: dict[str, tuple[str, ...]] = {
    "global": ("global", "worldwide", "anywhere", "international", "any geography"),
    "emerging markets": ("emerging markets", "emerging market", "frontier markets",
                         "developing markets", "global south"),
    "africa": ("africa", "pan-african", "pan african", "african", "continent-wide",
               "across africa"),
    "sub-saharan africa": ("sub-saharan africa", "sub saharan africa", "ssa"),
    "west africa": ("west africa", "western africa", "west african"),
    "anglophone africa": ("anglophone africa", "english-speaking africa"),
    "nigeria": ("nigeria", "nigerian", "lagos", "abuja"),
    "kenya": ("kenya", "kenyan", "nairobi"),
    "south africa": ("south africa", "south african", "cape town", "johannesburg"),
    "egypt": ("egypt", "egyptian", "cairo"),
    "ghana": ("ghana", "ghanaian", "accra"),
}


def _canonical(value: str, table: dict[str, tuple[str, ...]]) -> str | None:
    """Longest synonym wins, so 'pre-seed' never matches as 'seed'."""
    text = re.sub(r"[^a-z0-9\s'-]", " ", (value or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    best: tuple[int, str] | None = None
    for canon, synonyms in table.items():
        for syn in synonyms:
            if re.search(rf"(?<![a-z0-9]){re.escape(syn)}(?![a-z0-9])", text):
                if best is None or len(syn) > best[0]:
                    best = (len(syn), canon)
    return best[1] if best else None


def canonical_stage(value: str) -> str | None:
    return _canonical(value, STAGE_SYNONYMS)


def canonical_sector(value: str) -> str | None:
    return _canonical(value, SECTOR_SYNONYMS)


def canonical_geo(value: str) -> str | None:
    return _canonical(value, GEO_SYNONYMS)


def normalise_all(values: list[str], fn) -> set[str]:
    return {c for c in (fn(v) for v in values or []) if c}


def geo_covers(target_geo: str, sender_geo: str) -> bool:
    """Does a fund saying `target_geo` invest somewhere `sender_geo` is?

    'africa' covers 'nigeria'. 'kenya' does not.
    """
    target = canonical_geo(target_geo) or (target_geo or "").lower().strip()
    sender = canonical_geo(sender_geo) or (sender_geo or "").lower().strip()
    if not target or not sender:
        return False
    if target == sender:
        return True
    return sender in GEO_COVERS.get(target, set())


def stage_matches(target_stages: list[str], sender_stage: str) -> bool:
    wanted = canonical_stage(sender_stage)
    if not wanted:
        return False
    return wanted in normalise_all(target_stages, canonical_stage)


def stage_floor_above(target_stages: list[str], sender_stage: str) -> bool:
    """True when the fund's earliest stage is later than the sender's.

    A fund whose floor is Series A cannot be talked into a pre-seed, so
    this is a hard rejection rather than a low score.
    """
    wanted = canonical_stage(sender_stage)
    canon = normalise_all(target_stages, canonical_stage)
    if not wanted or not canon:
        return False
    try:
        earliest = min(STAGE_ORDER.index(s) for s in canon if s in STAGE_ORDER)
    except ValueError:
        return False
    return earliest > STAGE_ORDER.index(wanted)
