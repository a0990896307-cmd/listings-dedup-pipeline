"""Tests for geo comparable search and quality metrics.

The assertions target the two ways this feature can mislead: a comparable
set that hides how coarse it is, and a quality figure computed without
saying how it was computed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.geo import (
    DedupQuality,
    completeness,
    cost_per_1k_usable,
    coverage,
    find_comparables,
)

failures: list[str] = []
checks = 0


def check(name: str, actual, expected) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{name}\n     expected: {expected!r}\n     actual:   {actual!r}")


def check_true(name: str, value) -> None:
    check(name, bool(value), True)


def row(lid, lat=None, lon=None, **kw):
    d = {"listing_id": lid, "lat": lat, "lon": lon, "address": f"Via {lid}",
         "geo_precision": "exact", "source_id": 1, "comune": "Bari"}
    d.update(kw)
    return d


# Roughly 0.0009 degrees latitude is about 100 m.
POOL = [
    row(1, 41.11710, 16.87190),   # ~10 m
    row(2, 41.11750, 16.87190),   # ~45 m
    row(3, 41.11810, 16.87190),   # ~110 m
    row(4, 41.11950, 16.87190),   # ~270 m
    row(5, 41.12300, 16.87190),   # ~655 m
    row(6, 41.13000, 16.87190),   # ~1440 m
]

res = find_comparables(41.1171, 16.8719, POOL)
check_true("found some comparables", res["total_found"] > 0)
check("sorted by distance",
      [r["distance_m"] for r in res["results"]],
      sorted(r["distance_m"] for r in res["results"]))

# Radius attribution: each result reports the ring that captured it.
first = res["results"][0]
check("closest is inside first ring", first["found_at_radius_m"], 50.0)
check_true("closest is genuinely close", first["distance_m"] < 50)

per = res["found_per_radius_m"]
check_true("per-radius counts sum to total",
           sum(per.values()) >= res["total_found"])
check("first ring has the close ones", int(per["50.0"]), 2)
check_true("far ones excluded", int(per["50.0"]) < len(POOL))

# Every result must know its radius, otherwise precision is guesswork.
check_true("all results carry a radius",
           all(r["found_at_radius_m"] in res["radii_used_m"]
               for r in res["results"]))

# Exclusions must work, or a property compares against itself.
res2 = find_comparables(41.1171, 16.8719, POOL, exclude_ids=[1, 2])
check("excluded ids absent",
      any(r["listing_id"] in (1, 2) for r in res2["results"]), False)

# Missing coordinates cannot produce a distance.
POOL_NOGEO = POOL + [row(99, None, None)]
res3 = find_comparables(41.1171, 16.8719, POOL_NOGEO)
check("no-geo row excluded",
      any(r["listing_id"] == 99 for r in res3["results"]), False)

# max_results must report that it truncated rather than silently dropping.
res4 = find_comparables(41.1171, 16.8719, POOL, max_results=2)
check("max_results honoured", len(res4["results"]), 2)
check("truncation flagged", res4["truncated"], True)

# Price separation: asking and auction must never be conflated.
POOL_PRICE = [
    row(1, 41.11710, 16.87190, asking_price_cents=150000),
    row(2, 41.11750, 16.87190, auction_price_cents=90000),
]
res5 = find_comparables(41.1171, 16.8719, POOL_PRICE)
by_id = {r["listing_id"]: r for r in res5["results"]}
check("asking labelled", by_id[1]["price_source"], "asking")
check("auction labelled", by_id[2]["price_source"], "auction")
check("asking not copied into auction field",
      by_id[1]["auction_price_cents"], None)
check("auction not copied into asking field",
      by_id[2]["asking_price_cents"], None)

# An auction result inside 100m must drop to medium confidence rather than
# read as a high-confidence comparable.
check("auction caps confidence",
      by_id[2]["confidence"] in ("medium", "low"), True)

# Approximate coordinates must lower confidence at the same distance.
POOL_PREC = [
    row(1, 41.11710, 16.87190, geo_precision="exact"),
    row(2, 41.11720, 16.87190, geo_precision="approx"),
]
res6 = find_comparables(41.1171, 16.8719, POOL_PREC)
prec = {r["listing_id"]: r for r in res6["results"]}
check("exact beats approx on confidence",
      prec[1]["confidence"] in ("high", "medium"), True)
check("approx flagged low", prec[2]["confidence"], "low")

check_true("precision mix reported",
           set(res6["geo_precision_mix"].keys()) >= {"exact", "approx"})

# No pool at all must be a clean empty answer, not a crash.
empty = find_comparables(41.1171, 16.8719, [])
check("empty pool returns zero", empty["total_found"], 0)
check("empty pool quality", empty["comparable_set_quality"], "good")
check_true("warning always present", bool(empty["warning"]))
check_true("warning disclaims valuation",
           "not a valuation" in empty["warning"])

# --------------------------------------------------------------- metrics

RECS = [
    {"source_id": 1, "comune": "Bari", "last_seen": "2026-10-01",
     "address_raw": "a", "lat": 41.1, "lon": 16.8, "building_area_sqm": 80,
     "rooms": 3, "asking_price_cents": 100, "property_type": "flat"},
    {"source_id": 1, "comune": "Bari", "last_seen": "2026-10-01",
     "address_raw": "b", "lat": 41.1, "lon": 16.8, "building_area_sqm": None,
     "rooms": None, "asking_price_cents": None, "property_type": None},
    {"source_id": 2, "comune": "Monopoli", "last_seen": "2026-10-02",
     "address_raw": "c", "lat": None, "lon": None, "building_area_sqm": 60,
     "rooms": 2, "asking_price_cents": 90, "property_type": "house"},
]

comp = completeness(RECS)
check("area completeness", comp["building_area_sqm"]["share"], round(2/3, 4))
check("rooms completeness", comp["rooms"]["share"], round(2/3, 4))
check("lat completeness", comp["lat"]["share"], round(2/3, 4))
check_true("per-field reporting", "address_raw" in comp)

cov = coverage(RECS, target_comune="Bari")
check("total records", cov["total_records"], 3)
check("in target", cov["in_target"], 2)
check("target share", cov["target_share"], round(2/3, 4))
check_true("per-comune breakdown", "Monopoli" in cov["per_comune"])

check("cost per 1k", cost_per_1k_usable(500, 2), 250000.0)
check("cost per 1k with nothing usable", cost_per_1k_usable(500, 0), None)

# Auto-merges are unreviewed by definition, so the false-merge rate has
# to come from an audit sample, not from the review queue. Conflating the
# two would report a number about a population nobody checked.
q = DedupQuality(pairs=100, merged=20, auto_merged_sampled=10,
                 auto_merged_false=2, reviewed_confirmed=8, reviewed_rejected=2)
d = q.to_dict()
check("auto merge rate", d["auto_merge_rate"], 0.2)
check("false merge rate from audit", q.false_merge_rate, 0.2)
check("candidate precision from review", q.candidate_precision, 0.8)
check_true("populations are distinguished",
           "different populations" in d["note"])

# Without an audit there is no false-merge figure at all, rather than one
# borrowed from a different population.
check("no false merge rate without audit",
      DedupQuality(pairs=10, merged=5).false_merge_rate, None)
check("no candidate precision without review",
      DedupQuality(pairs=10).candidate_precision, None)

# A missed duplicate leaves no row, so it cannot be read off the log.
check_true("miss rate declared unavailable",
           DedupQuality().missed_duplicate_rate is None)


# ------------------------------------------------------------------ report

print()
if failures:
    print(f"FAILED {len(failures)}/{checks}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED all {checks} checks")