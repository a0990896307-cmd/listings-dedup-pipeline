"""Geo comparable search and quality metrics.

Two requirements from the brief drive the design:

- comparable search starts at 50m and expands, and reports which radius
  produced each result, so the analyst sees the precision they are
  working at instead of trusting a point
- false merges and missed duplicates must both be measurable, because
  they are not the same kind of error

Nothing here computes a "market value". Asking prices and auction
valuations are kept apart, and the API refuses to average them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from .dedup import Decision, haversine_m

EARTH_RADIUS_M = 6_371_000.0

# Radii the comparable search walks through. Each step roughly quadruples
# area coverage while staying inside a plausible walkable/driveable
# comparable distance for property in an Italian city.
DEFAULT_RADII_M = (50.0, 100.0, 200.0, 400.0, 800.0)


@dataclass
class Comparable:
    listing_id: int
    address: str
    lat: float
    lon: float
    distance_m: float
    found_at_radius_m: float
    geo_precision: str
    asking_price_cents: int | None = None
    auction_price_cents: int | None = None
    building_area_sqm: float | None = None
    rooms: int | None = None
    property_type: str | None = None
    first_seen: str = ""
    last_seen: str = ""

    @property
    def price_source(self) -> str:
        """Which kind of price this is, stated on every row.

        An analyst comparing a reno target needs to know whether the
        number beside it was an asking price or an auction valuation,
        because those behave differently in a downturn.
        """
        if self.auction_price_cents is not None:
            return "auction"
        return "asking"

    def confidence(self, radius_m: float) -> str:
        """Confidence indicator, never a single opaque score.

        Combines how far away it is, how trustworthy the coordinates are,
        and whether the price is an asking price or a realised one.
        """
        if self.geo_precision == "approx":
            base = "low"
        elif radius_m <= 100:
            base = "high"
        elif radius_m <= 400:
            base = "medium"
        else:
            base = "low"

        if self.price_source == "auction" and base == "high":
            base = "medium"
        return base

    def to_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "address": self.address,
            "distance_m": round(self.distance_m, 1),
            "found_at_radius_m": self.found_at_radius_m,
            "geo_precision": self.geo_precision,
            "confidence": self.confidence(self.found_at_radius_m),
            "asking_price_cents": self.asking_price_cents,
            "auction_price_cents": self.auction_price_cents,
            "price_source": self.price_source,
            "building_area_sqm": self.building_area_sqm,
            "rooms": self.rooms,
            "property_type": self.property_type,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def find_comparables(
    lat: float,
    lon: float,
    pool: Sequence[dict],
    radii_m: Sequence[float] = DEFAULT_RADII_M,
    exclude_ids: Sequence = (),
    max_results: int = 40,
) -> dict:
    """Expand outward from 50m and report what each radius found.

    Each comparable is attributed to the first radius that captured it, so
    a run returns "3 within 50m, 5 more within 200m" rather than a flat
    list where distance has to be re-derived by eye.
    """
    excluded = set(exclude_ids)
    out: list[Comparable] = []
    seen: set[int] = set()
    per_radius: dict[float, int] = {}
    precision_mix: dict[str, int] = {}

    for radius in radii_m:
        found = 0
        for row in pool:
            # Listing ids are strings in some sources and integers in the
            # database. Keep whatever the caller supplied rather than
            # forcing a conversion that raises on a legitimate id.
            lid = row["listing_id"]
            if lid in seen or lid in excluded:
                continue
            rlat, rlon = row.get("lat"), row.get("lon")
            if rlat is None or rlon is None:
                continue
            d = haversine_m(lat, lon, float(rlat), float(rlon))
            if d > radius:
                continue
            seen.add(lid)
            found += 1
            prec = row.get("geo_precision", "unknown")
            precision_mix[prec] = precision_mix.get(prec, 0) + 1
            out.append(
                Comparable(
                    listing_id=lid,
                    address=row.get("address", ""),
                    lat=float(rlat),
                    lon=float(rlon),
                    distance_m=d,
                    found_at_radius_m=radius,
                    geo_precision=prec,
                    asking_price_cents=row.get("asking_price_cents"),
                    auction_price_cents=row.get("auction_price_cents"),
                    building_area_sqm=row.get("building_area_sqm"),
                    rooms=row.get("rooms"),
                    property_type=row.get("property_type"),
                    first_seen=str(row.get("first_seen", "")),
                    last_seen=str(row.get("last_seen", "")),
                )
            )
        per_radius[radius] = found

    out.sort(key=lambda c: c.distance_m)
    truncated = len(out) > max_results
    out = out[:max_results]

    # A comparable set is only as good as its weakest coordinate. Saying so
    # is more useful than a number the analyst cannot interpret.
    quality = "good"
    if precision_mix.get("unknown", 0) > len(out) / 2 if out else False:
        quality = "poor"
    elif precision_mix.get("approx", 0) > len(out) / 2 if out else False:
        quality = "fair"

    return {
        "centre": {"lat": lat, "lon": lon},
        "radii_used_m": list(radii_m),
        "found_per_radius_m": {str(k): v for k, v in per_radius.items()},
        "total_found": len(out),
        "truncated": truncated,
        "geo_precision_mix": precision_mix,
        "comparable_set_quality": quality,
        "warning": (
            "This is a comparable set of asking prices, not a valuation. "
            "Asking prices and auction valuations are reported separately "
            "and neither is a completed-sale price."
        ),
        "results": [c.to_dict() for c in out],
    }


# --------------------------------------------------------------- metrics

@dataclass
class DedupQuality:
    """Both error directions, measured from the decision log.

    Two separate measurements, because they come from different evidence:

    - `auto_merged_sampled` / `auto_merged_false` come from auditing
      automatic merges. An automatic merge is by definition unreviewed,
      so the only way to know its error rate is to sample and check.
    - `reviewed_confirmed` / `reviewed_rejected` come from the candidate
      queue and measure how precise the candidate ranking is.

    Conflating the two would produce a confident number about a
    population nobody checked.
    """

    pairs: int = 0
    by_decision: dict = field(default_factory=dict)
    merged: int = 0

    # Audit of automatic merges.
    auto_merged_sampled: int = 0
    auto_merged_false: int = 0

    # Outcomes from the review queue.
    reviewed_confirmed: int = 0
    reviewed_rejected: int = 0
    pending_review: int = 0

    @property
    def false_merge_rate(self) -> float | None:
        """Share of audited automatic merges that were wrong.

        None when nothing has been audited. A number here without an audit
        behind it would be a guess presented as a measurement.
        """
        if self.auto_merged_sampled <= 0:
            return None
        return round(self.auto_merged_false / self.auto_merged_sampled, 4)

    @property
    def candidate_precision(self) -> float | None:
        """Share of reviewed candidates that really were duplicates."""
        reviewed = self.reviewed_confirmed + self.reviewed_rejected
        if reviewed <= 0:
            return None
        return round(self.reviewed_confirmed / reviewed, 4)

    @property
    def missed_duplicate_rate(self) -> float | None:
        """Share of true duplicates never proposed for review at all.

        Cannot be derived from the decision log alone, because a pair that
        was never compared leaves no row. Requires a labelled negative
        sample: known-duplicate pairs held back from the crawler. The
        validation phase produces that sample.
        """
        return None

    def to_dict(self) -> dict:
        return {
            "pairs_compared": self.pairs,
            "by_decision": self.by_decision,
            "auto_merged": self.merged,
            "auto_merge_rate": round(self.merged / self.pairs, 4) if self.pairs else 0.0,
            "auto_merged_sampled": self.auto_merged_sampled,
            "false_merge_rate": self.false_merge_rate,
            "candidate_confirmed": self.reviewed_confirmed,
            "candidate_rejected": self.reviewed_rejected,
            "candidate_precision": self.candidate_precision,
            "pending_review": self.pending_review,
            "missed_duplicate_rate": self.missed_duplicate_rate,
            "note": (
                "false_merge_rate covers audited automatic merges only. "
                "candidate_precision covers the review queue. They are "
                "different populations and are not interchangeable. "
                "missed_duplicate_rate requires a labelled negative sample "
                "and is unavailable from the decision log alone."
            ),
        }


def freshness(records: Sequence[dict], now_iso: str, stale_after_hours: int = 48) -> dict:
    """How stale the newest data is, per source."""
    by_source: dict[str, list[str]] = {}
    for r in records:
        sid = str(r.get("source_id", "?"))
        seen = str(r.get("last_seen", ""))
        if seen:
            by_source.setdefault(sid, []).append(seen)

    out = {}
    for sid, stamps in sorted(by_source.items()):
        newest = max(stamps)
        out[sid] = {"newest_last_seen": newest, "stale_after_hours": stale_after_hours}
    return out


def coverage(records: Sequence[dict], target_comune: str | None = None) -> dict:
    """Records per comune, plus the share inside the target area."""
    counts: dict[str, int] = {}
    for r in records:
        c = r.get("comune") or "?"
        counts[c] = counts.get(c, 0) + 1
    total = sum(counts.values())
    return {
        "total_records": total,
        "per_comune": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "target_comune": target_comune,
        "in_target": counts.get(target_comune, 0) if target_comune else None,
        "target_share": (
            round(counts.get(target_comune, 0) / total, 4)
            if target_comune and total else None
        ),
    }


def completeness(records: Sequence[dict]) -> dict:
    """Share of key fields present, per field.

    Reported per field because a single aggregate completeness number
    hides which attribute is actually the problem.
    """
    fields = [
        "address_raw", "comune", "lat", "lon", "building_area_sqm",
        "rooms", "asking_price_cents", "property_type",
    ]
    total = len(records)
    out = {}
    for f in fields:
        present = sum(1 for r in records if r.get(f) not in (None, ""))
        out[f] = {
            "present": present,
            "total": total,
            "share": round(present / total, 4) if total else 0.0,
        }
    return out


def cost_per_1k_usable(total_cost_cents: int, usable_records: int) -> float | None:
    """The figure the pilot is measured on.

    "Usable" means: passed validation, has a civic number so it can be
    deduplicated, and has coordinates. A record without those cannot
    produce a comparable, so counting it would flatter the number.
    """
    if usable_records <= 0:
        return None
    return round(total_cost_cents / usable_records * 1000, 2)