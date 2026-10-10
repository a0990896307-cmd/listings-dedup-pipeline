"""Cross-agency deduplication with traceable decisions.

Three tiers, each stricter than the last:

1. Exact: normalised address key plus commune match. No judgement.
2. Geographic: within a distance threshold, with area and room count
   close enough that the two listings cannot plausibly be neighbours
   rather than the same building.
3. Fuzzy: similarity above a threshold on text fields. Logged, never
   auto-accepted, because a wrong merge here silently corrupts the
   comparable set.

Every decision carries the evidence that produced it. A false merge costs
a comparable sale; a missed duplicate costs a data point. Those are not
symmetric, so the log has to let you measure both from the same place
rather than picking one number to optimise.

Nothing is merged automatically above tier 2. Tier 3 produces review
candidates, which is the only honest way to handle ambiguous cases without
a human labelling thousands of pairs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Iterable, Sequence

from rapidfuzz import fuzz

from .address import Address, normalise, street_similarity

EARTH_RADIUS_M = 6_371_000.0

# Thresholds below are deliberately conservative. A missed duplicate is a
# visible gap in the database; a false merge is an invisible wrong number
# in a comparable analysis. The cost is asymmetric, so bias toward recall
# into review rather than toward automatic merges.

MIN_FUZZY_STREET = 0.86      # street name must be near-identical
MIN_FUZZY_EXTRA = 0.70        # at least one supporting field must agree
MAX_GEO_DISTANCE_M = 40.0     # same building, not next door
AREA_TOLERANCE = 0.15         # 15% area difference
ROOMS_TOLERANCE = 1           # integer room difference


class Decision(str, Enum):
    EXACT = "exact"            # merged with no judgement
    GEO = "geo"                # merged, evidence recorded
    CANDIDATE = "candidate"    # logged for review, not merged
    REJECT = "reject"


@dataclass
class Listing:
    id: str
    address_raw: str
    comune: str = ""
    lat: float | None = None
    lon: float | None = None
    area_sqm: float | None = None
    rooms: int | None = None
    floor: int | None = None
    asking_price: float | None = None
    agency: str = ""
    url: str = ""
    source_id: str = ""
    last_seen: str = ""
    address: Address = field(default=None, repr=False, compare=False)  # type: ignore

    def __post_init__(self) -> None:
        if self.address is None:
            self.address = normalise(self.address_raw, self.comune)

    @property
    def has_geo(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("address", None)
        d["address_parsed"] = self.address.to_dict()
        return d


@dataclass
class MatchResult:
    left_id: str
    right_id: str
    decision: Decision
    score: float
    tier: int
    evidence: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def merged(self) -> bool:
        return self.decision in (Decision.EXACT, Decision.GEO)

    def to_dict(self) -> dict:
        return {
            "left_id": self.left_id,
            "right_id": self.right_id,
            "decision": self.decision.value,
            "score": round(self.score, 4),
            "tier": self.tier,
            "merged": self.merged,
            "evidence": self.evidence,
            "reasons": self.reasons,
        }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _area_close(a: float | None, b: float | None) -> bool | None:
    if a is None or b is None:
        return None
    if min(a, b) <= 0:
        return None
    diff = abs(a - b) / max(a, b)
    return diff <= AREA_TOLERANCE


def _rooms_close(a: int | None, b: int | None) -> bool | None:
    if a is None or b is None:
        return None
    return abs(a - b) <= ROOMS_TOLERANCE


def compare(a: Listing, b: Listing) -> MatchResult:
    """Compare one pair. Pure function, no side effects."""
    if a.id == b.id:
        return MatchResult(a.id, b.id, Decision.REJECT, 0.0, 0,
                           reasons=["same listing"])

    reasons: list[str] = []
    evidence: dict = {}

    # --- tier 1: exact ---------------------------------------------------
    ka, kb = a.address.key, b.address.key
    evidence["address_key"] = {"a": ka, "b": kb}

    if ka and kb and ka == kb:
        # A matching address is necessary but not sufficient. Two units on
        # the same civic number with wildly different area are two units,
        # and merging them on address alone would drop a real property out
        # of the comparable set. Contradiction vetoes the merge at every
        # tier, including exact.
        area_ok = _area_close(a.area_sqm, b.area_sqm)
        rooms_ok = _rooms_close(a.rooms, b.rooms)
        contradictions = sum(1 for x in (area_ok, rooms_ok) if x is False)
        evidence["area_compatible"] = area_ok
        evidence["rooms_compatible"] = rooms_ok
        evidence["contradictions"] = contradictions

        if contradictions:
            reasons.append("address matches but attributes conflict")
            return MatchResult(a.id, b.id, Decision.CANDIDATE, 1.0, 1,
                               evidence=evidence, reasons=reasons)

        if a.agency and b.agency and a.agency != b.agency:
            reasons.append("same address, different agency")
        return MatchResult(a.id, b.id, Decision.EXACT, 1.0, 1,
                           evidence=evidence, reasons=reasons)

    # --- tier 2: geographic ----------------------------------------------
    street_sim = street_similarity(a.address, b.address)
    evidence["street_similarity"] = round(street_sim, 4)

    distance_m = None
    if a.has_geo and b.has_geo:
        distance_m = haversine_m(a.lat, b.lon, b.lat, b.lon)
        evidence["distance_m"] = round(distance_m, 2)
        evidence["geo_precision_flag"] = True  # see note in README

    area_ok = _area_close(a.area_sqm, b.area_sqm)
    rooms_ok = _rooms_close(a.rooms, b.rooms)
    evidence["area_compatible"] = area_ok
    evidence["rooms_compatible"] = rooms_ok

    civic_match = bool(a.address.civic and a.address.civic == b.address.civic)
    evidence["civic_match"] = civic_match

    if distance_m is not None and distance_m <= MAX_GEO_DISTANCE_M:
        # Two listings with different civic numbers are different units in
        # the same building, not the same property. Proximity and similar
        # attributes are exactly what you would expect between them, so
        # merging here would silently destroy a real unit from the
        # comparable set. They go to review instead.
        civic_conflict = bool(
            a.address.civic and b.address.civic and not civic_match
        )
        evidence["civic_conflict"] = civic_conflict

        if civic_conflict:
            reasons.append(
                f"same building but different civic numbers "
                f"({a.address.civic} vs {b.address.civic})"
            )
            return MatchResult(a.id, b.id, Decision.CANDIDATE,
                               round(street_sim, 4), 3,
                               evidence=evidence, reasons=reasons)

        # Same building requires agreeing on more than one dimension, or a
        # single very high street score. Two listings can share a street and
        # a postcode and still be different buildings.
        supporting = sum(
            1 for x in (area_ok, rooms_ok, civic_match) if x is True
        )
        unknown = sum(1 for x in (area_ok, rooms_ok) if x is None)

        # An explicit disagreement is a veto, not a neutral. A 300 sqm
        # eight-room listing and an 85 sqm three-room listing on the same
        # civic number are different properties, and proximity plus a
        # matching civic would happily merge them if contradictions were
        # merely ignored. Only missing data is tolerated; known-bad data
        # is disqualifying.
        contradictions = sum(1 for x in (area_ok, rooms_ok) if x is False)
        evidence["contradictions"] = contradictions

        if contradictions:
            reasons.append("attribute conflict rules out an automatic merge")
            return MatchResult(a.id, b.id, Decision.CANDIDATE,
                               round(street_sim, 4), 3,
                               evidence=evidence, reasons=reasons)

        enough = street_sim >= 0.95 and supporting >= 1
        strong = street_sim >= 0.88 and supporting >= 2 and unknown == 0

        if enough or strong:
            reasons.append(f"within {distance_m:.0f}m")
            if civic_match:
                reasons.append("matching civic number")
            if area_ok:
                reasons.append("compatible area")
            if rooms_ok:
                reasons.append("compatible rooms")
            score = min(1.0, street_sim * 0.6 + (1 - distance_m / (MAX_GEO_DISTANCE_M * 10)) * 0.4)
            return MatchResult(a.id, b.id, Decision.GEO, round(score, 4), 2,
                               evidence=evidence, reasons=reasons)

    # --- tier 3: fuzzy, review only --------------------------------------
    extras_ok = 0
    extras_seen = 0
    if a.area_sqm and b.area_sqm:
        extras_seen += 1
        if _area_close(a.area_sqm, b.area_sqm):
            extras_ok += 1
    if a.rooms is not None and b.rooms is not None:
        extras_seen += 1
        if _rooms_close(a.rooms, b.rooms):
            extras_ok += 1

    text_score = fuzz.token_set_ratio(
        f"{a.address_raw} {a.comune}", f"{b.address_raw} {b.comune}"
    ) / 100.0
    evidence["text_similarity"] = round(text_score, 4)
    evidence["extra_fields_agreeing"] = extras_ok
    evidence["extra_fields_compared"] = extras_seen

    price_hint = False
    if a.asking_price and b.asking_price:
        lo, hi = sorted((a.asking_price, b.asking_price))
        if hi > 0:
            ratio = lo / hi
            price_hint = ratio >= 0.85
    evidence["price_consistent"] = price_hint

    enough_support = extras_ok >= 1 if extras_seen else bool(price_hint)
    if street_sim >= MIN_FUZZY_STREET and enough_support:
        reasons.append(f"street similarity {street_sim:.2f}")
        if extras_ok:
            reasons.append("one or more attributes agree")
        if price_hint:
            reasons.append("asking prices consistent")
        return MatchResult(a.id, b.id, Decision.CANDIDATE,
                           round(street_sim, 4), 3,
                           evidence=evidence, reasons=reasons)

    return MatchResult(a.id, b.id, Decision.REJECT, round(street_sim, 4), 3,
                       evidence=evidence, reasons=["below thresholds"])


class Deduplicator:
    """Blocking by civic number, then pairwise comparison inside blocks.

    Comparing every pair is quadratic and pointless: two listings on
    different streets cannot be the same property. Blocking on the civic
    number reduces a 50k-listing crawl from ~1.2 billion comparisons to
    something that finishes in minutes, without weakening recall, since a
    true duplicate always shares a civic number.
    """

    def __init__(self, max_block_size: int = 50) -> None:
        self.max_block_size = max_block_size

    def blocks(self, listings: Sequence[Listing]) -> dict[str, list[Listing]]:
        out: dict[str, list[Listing]] = {}
        for l in listings:
            civic = l.address.civic
            comune = l.address.comune or l.comune
            if not civic:
                continue
            key = f"{comune}|{civic}"
            out.setdefault(key, []).append(l)
        return out

    def run(self, listings: Sequence[Listing]) -> list[MatchResult]:
        results: list[MatchResult] = []
        for key, group in self.blocks(listings).items():
            if len(group) < 2:
                continue
            if len(group) > self.max_block_size:
                # A block this large is usually a badly parsed civic
                # number, not a real building. Comparing 200 listings
                # pairwise would produce noise, so it is skipped loudly
                # rather than silently.
                results.append(
                    MatchResult(key, key, Decision.REJECT, 0.0, 0,
                                reasons=[f"block too large ({len(group)}), "
                                         "likely a parsing error"])
                )
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    results.append(compare(group[i], group[j]))
        return results

    def summary(self, results: Sequence[MatchResult]) -> dict:
        """Counts needed to measure both error types from the same log."""
        by_decision: dict[str, int] = {}
        for r in results:
            by_decision[r.decision.value] = by_decision.get(r.decision.value, 0) + 1
        merged = [r for r in results if r.merged]
        candidates = [r for r in results if r.decision is Decision.CANDIDATE]
        return {
            "pairs_compared": len(results),
            "by_decision": by_decision,
            "merged": len(merged),
            "review_queue": len(candidates),
            "auto_merge_rate": round(len(merged) / len(results), 4) if results else 0.0,
        }