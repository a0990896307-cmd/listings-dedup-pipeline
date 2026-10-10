#!/usr/bin/env python
"""End-to-end demonstration of the Bari pilot pipeline.

Runs with no database, no API keys and no network. This is the smoke test:
a reviewer should be able to run it and see the deduplication decisions
before reading any code.

    python demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.address import street_similarity
from app.dedup import Decision, Deduplicator, Listing, compare
from app.geo import completeness, cost_per_1k_usable, find_comparables
from app.ingest import Ingestor, InMemoryDB

# One building in Bari, written the way five agencies write it. Four are
# the same apartment; the fifth is a penthouse in the same civic number.
LISTINGS = [
    ("Immobiliare", "Via Giuseppe Verdi 12", 41.11710, 16.87190, 85, 3, 150000),
    ("Idealista",   "V. G. Verdi, 12",      41.11712, 16.87190, 85, 3, 152000),
    ("Subito",      "VIA VERDI 12",          41.11711, 16.87191, 87, 3, 148000),
    ("Trovit",      "Via Verdi n. 12",      41.11713, 16.87192, 85, 3, None),
    ("GreenHome",   "Verdi 12",              41.11715, 16.87192, 300, 8, 420000),
    ("Cerchi",      "Via Milano 5",          41.12000, 16.87190, 70, 2, 110000),
    ("Abruzzo",     "Lungomare Starita 9",   41.12500, 16.87200, 95, 4, 210000),
]


def build() -> list[Listing]:
    out = []
    for i, (agency, addr, lat, lon, area, rooms, price) in enumerate(LISTINGS):
        out.append(Listing(
            id=f"{agency.lower()}-{i}",
            address_raw=addr,
            comune="Bari",
            lat=lat, lon=lon,
            area_sqm=area, rooms=rooms,
            agency=agency,
            asking_price=price,
            url=f"https://example.invalid/{agency.lower()}/{i}",
            source_id=f"src-{agency.lower()}",
            last_seen="2026-10-01",
        ))
    return out


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> int:
    listings = build()

    section("1. NORMALISED ADDRESSES")
    for l in listings:
        a = l.address
        print(f"  {l.agency:12} {l.address_raw:24} -> {a.key or '(no usable key)'}")

    section("2. WHY NORMALISATION IS NOT ENOUGH ON ITS OWN")
    base = listings[0]
    for l in listings[1:4]:
        same = l.address.key == base.address.key
        print(f"  {l.agency:12} {l.address_raw:20}")
        print(f"                 exact key match: {same}")
        print(f"                 street similarity to Immobiliare: "
              f"{street_similarity(base.address, l.address):.2f}")
    print()
    print("  'Giuseppe Verdi' and 'G. Verdi' are the same street but do not")
    print("  produce the same exact key, so tier 1 does not fire for them.")
    print("  Tier 2 catches them on similarity plus coordinates and attributes.")
    print("  That split is deliberate: normalise what is exactly normalisable,")
    print("  and let evidence handle the rest.")

    section("3. PAIRWISE DECISIONS")
    dedup = Deduplicator()
    results = dedup.run(listings)
    label = {
        Decision.EXACT: "MERGE  (tier 1)",
        Decision.GEO: "MERGE  (tier 2)",
        Decision.CANDIDATE: "REVIEW (tier 3)",
        Decision.REJECT: "no match",
    }
    for r in results:
        print(f"  {r.left_id:14} vs {r.right_id:14} {label[r.decision]}")
        for reason in r.reasons:
            print(f"                   {reason}")

    section("4. SUMMARY")
    for k, v in dedup.summary(results).items():
        print(f"  {k:20} {v}")

    section("5. FALSE MERGE IS BLOCKED")
    a = listings[0]
    penthouse = listings[4]
    r = compare(a, penthouse)
    print(f"  {a.agency} ({a.area_sqm} sqm, {a.rooms} rooms)")
    print(f"  {penthouse.agency} ({penthouse.area_sqm} sqm, {penthouse.rooms} rooms)")
    print(f"  decision: {r.decision.value}  ->  {'; '.join(r.reasons)}")
    print("  Both share a civic number and sit 6 m apart, so proximity alone")
    print("  would have merged them.")

    section("6. INGESTION")
    db = InMemoryDB()
    ing = Ingestor(db, source_id=1, backoff_seconds=0)

    def page():
        return [
            {"source_listing_id": l.source_id, "url": l.url,
             "address_raw": l.address_raw, "comune": "Bari",
             "lat": l.lat, "lon": l.lon, "geo_precision": "exact",
             "area_sqm": l.area_sqm, "rooms": l.rooms,
             "asking_price_cents": l.asking_price, "agency": l.agency}
            for l in listings
        ]

    def broken():
        return [
            {"url": "no source id", "address_raw": "Via Napoli 1"},
            {"source_listing_id": "half-geo", "url": "u",
             "address_raw": "Via Torino 3", "lat": 41.1},
        ]

    run = ing.ingest([page, broken], cost_cents=520)
    for k, v in run.to_dict().items():
        print(f"  {k:24} {v}")

    print()
    print("  Two records quarantined with a reason rather than dropped:")
    for row in db.quarantine_rows:
        print(f"    {row['reason']}")

    section("7. RE-RUN IS IDEMPOTENT")
    run2 = ing.ingest([page], cost_cents=0)
    print(f"  listings in database      {len(db.listings)}")
    print(f"  distinct properties       {len(db.properties)}")
    print(f"  new properties this run   {run2.new_properties}")
    print(f"  price changes this run    {run2.price_changes}")

    section("8. COMPARABLE SEARCH FROM 50 m")
    pool = [
        {"listing_id": l.id, "address": l.address_raw, "lat": l.lat,
         "lon": l.lon, "geo_precision": "exact",
         "building_area_sqm": l.area_sqm, "rooms": l.rooms,
         "asking_price_cents": int(l.asking_price) if l.asking_price else None,
         "auction_price_cents": None,
         "first_seen": "2026-09-01", "last_seen": l.last_seen}
        for l in listings
    ]
    res = find_comparables(41.11710, 16.87190, pool, max_results=5)
    print(f"  found per radius (m): {res['found_per_radius_m']}")
    print(f"  total found:           {res['total_found']}")
    print(f"  comparable quality:    {res['comparable_set_quality']}")
    print()
    for c in res["results"]:
        print(f"    {c['distance_m']:>7.1f} m  found at {c['found_at_radius_m']:>5.0f} m"
              f"  confidence={c['confidence']:6}  price={c['price_source']}"
              f"  {c['address']}")
    print()
    print(f"  {res['warning']}")

    section("9. COMPLETENESS AND COST")
    rows = [{"address_raw": l.address_raw, "comune": l.comune, "lat": l.lat,
             "lon": l.lon, "building_area_sqm": l.area_sqm, "rooms": l.rooms,
             "asking_price_cents": l.asking_price, "property_type": None}
            for l in listings]
    for field, stat in completeness(rows).items():
        print(f"  {field:22} {stat['present']}/{stat['total']}"
              f"  ({stat['share']:.0%})")
    print(f"  cost per 1,000 usable    {cost_per_1k_usable(run.cost_cents, run.records_stored)} cents")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())