"""Tests for ingestion: retries, quarantine, idempotency, price history.

The cases that matter here are the ones that corrupt a database quietly:
an empty string written where NULL belongs, a half coordinate pair, a
re-run that duplicates, and a price that silently stops being recorded.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import (
    Ingestor,
    InMemoryDB,
    QuarantineRecord,
    RunStatus,
    clean_value,
    normalise_record,
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


# ------------------------------------------------------------ value clean

check("empty string becomes None", clean_value(""), None)
check("whitespace becomes None", clean_value("   "), None)
check("dash becomes None", clean_value("-"), None)
check("n/a becomes None", clean_value("n/a"), None)
check("N/A becomes None", clean_value("N/A"), None)
check("zero is kept", clean_value(0), 0)
check("False is kept", clean_value(False), False)
check("real value kept", clean_value("Bari"), "Bari")
check("zero price kept", clean_value(0), 0)

# ---------------------------------------------------------- normalisation

ok, why = normalise_record(
    {"source_listing_id": "1", "url": "http://x/1",
     "address_raw": "Via Roma 12", "comune": "Bari", "area_sqm": 85},
    source_id=1,
)
check("valid record accepted", ok is not None, True)
check("no reason on success", why, None)
check("missing list populated", "rooms" in ok["missing_fields"], True)
check("present field not in missing", "area_sqm" in ok["missing_fields"], False)

# Missing required fields must be refused with a diagnosable reason.
for bad, expect in [
    ({"url": "u", "address_raw": "a"}, "source_listing_id"),
    ({"source_listing_id": "1", "address_raw": "a"}, "url"),
    ({"source_listing_id": "1", "url": "u"}, "address_raw"),
]:
    rec, reason = normalise_record(bad, source_id=1)
    check(f"refuses missing {expect}", rec, None)
    check_true(f"reason names {expect}", expect in reason)

# A required field present but empty must also be refused.
rec, reason = normalise_record(
    {"source_listing_id": "1", "url": "", "address_raw": "a"}, source_id=1)
check("empty required refused", rec, None)
check_true("empty reason is diagnosable", "url" in reason)

# Half a coordinate is worse than none: it produces silently wrong
# distances rather than a missing value.
rec, reason = normalise_record(
    {"source_listing_id": "1", "url": "u", "address_raw": "a",
     "lat": 41.1}, source_id=1)
check("half coordinate refused", rec, None)
check_true("half coordinate reason", "coordinate" in reason)

# No coordinates is fine, just flagged unknown.
rec, _ = normalise_record(
    {"source_listing_id": "1", "url": "u", "address_raw": "a"}, source_id=1)
check("absent coordinates are None", rec["lat"], None)
check("absent precision is unknown", rec["geo_precision"], "unknown")

# Precision must not be invented.
rec, _ = normalise_record(
    {"source_listing_id": "1", "url": "u", "address_raw": "a",
     "lat": 41.1, "lon": 16.8, "geo_precision": "precise!"}, source_id=1)
check("bogus precision downgraded", rec["geo_precision"], "unknown")

rec, _ = normalise_record(
    {"source_listing_id": "1", "url": "u", "address_raw": "a",
     "lat": 41.1, "lon": 16.8, "geo_precision": "approx"}, source_id=1)
check("valid precision kept", rec["geo_precision"], "approx")

# --------------------------------------------------------------- ingestion

db = InMemoryDB()
ing = Ingestor(db, source_id=1, backoff_seconds=0)

page = lambda: [
    {"source_listing_id": "1", "url": "http://x/1",
     "address_raw": "Via Roma 12", "comune": "Bari",
     "asking_price_cents": 150000, "area_sqm": 85},
    {"source_listing_id": "2", "url": "http://x/2",
     "address_raw": "Via Roma 14", "comune": "Bari",
     "asking_price_cents": 180000, "area_sqm": 90},
]

rep = ing.ingest([page], cost_cents=500)
check("run ok", rep.status, RunStatus.OK)
check("two stored", rep.records_stored, 2)
check("two properties", rep.new_properties, 2)
check("cost per 1k computed", rep.to_dict()["cost_per_1k_usable"], 250000)
check("usable count exposed", rep.to_dict()["usable_records"], 2)

# Re-running must not duplicate.
rep2 = ing.ingest([page])
check("rerun stores same count", rep2.records_stored, 2)
check("rerun creates no properties", rep2.new_properties, 0)
check("database still has two listings", len(db.listings), 2)
check("database still has two properties", len(db.properties), 2)

# Price change must be recorded as a new observation.
changed = lambda: [
    {"source_listing_id": "1", "url": "http://x/1",
     "address_raw": "Via Roma 12", "comune": "Bari",
     "asking_price_cents": 140000, "area_sqm": 85},
]
rep3 = ing.ingest([changed])
check("price change detected", rep3.price_changes, 1)
check("price history appended", len(db.price_history), 3)
check("history is append only",
      db.price_history[-1]["asking_price_cents"], 140000)

# An unchanged price must not create a spurious observation.
rep4 = ing.ingest([changed])
check("unchanged price is not a change", rep4.price_changes, 0)
check("history did not grow", len(db.price_history), 3)

# Quarantine: bad records are kept with a reason, not dropped.
bad_page = lambda: [
    {"url": "no id", "address_raw": "Via Roma 1"},
    {"source_listing_id": "9", "url": "u", "address_raw": "a", "lat": 41.1},
    {"source_listing_id": "3", "url": "http://x/3",
     "address_raw": "Via Milano 5", "comune": "Bari", "asking_price_cents": 90000},
]
rep5 = ing.ingest([bad_page])
check("bad records quarantined", rep5.records_quarantined, 2)
check("good record still stored", rep5.records_stored, 1)
check("quarantine retains reasons", len(db.quarantine_rows), 2)
check_true("quarantine reasons are specific",
           all(r["reason"] for r in db.quarantine_rows))

# Retries: a flaky source must recover and the attempt must be recorded.
calls = {"n": 0}

def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise TimeoutError("source timed out")
    return [{"source_listing_id": "4", "url": "http://x/4",
             "address_raw": "Via Napoli 3", "comune": "Bari"}]

rep6 = Ingestor(InMemoryDB(), source_id=1, backoff_seconds=0).ingest([flaky])
# 'partial', not 'ok': the run recovered, but the fact that it needed
# retries is exactly what the client wants to see in the run log rather
# than have it disappear behind a success.
check("recovered run is partial, not ok", rep6.status, RunStatus.PARTIAL)
check("attempted three times", calls["n"], 3)
check("failed attempts recorded", len(rep6.errors), 2)
check_true("errors are diagnosable",
           all("TimeoutError" in e for e in rep6.errors))
check("recovered data was stored", rep6.records_stored, 1)

# Exhausted retries must not be silent.
def dead():
    raise ConnectionError("blocked")

rep7 = Ingestor(InMemoryDB(), source_id=1, max_retries=2, backoff_seconds=0).ingest([dead])
check_true("dead source is partial or failed",
           rep7.status in (RunStatus.PARTIAL, RunStatus.FAILED))
check_true("dead source records error", len(rep7.errors) >= 2)
check("dead source stored nothing", rep7.records_stored, 0)

# Partial availability: one page fails, another succeeds.
db_mixed = InMemoryDB()
mixed = Ingestor(db_mixed, source_id=1, max_retries=1, backoff_seconds=0).ingest(
    [dead, page]
)
check("partial run is partial", mixed.status, RunStatus.PARTIAL)
check("good page still stored", mixed.records_stored, 2)

# Later pages win for a duplicated source id.
db_dup = InMemoryDB()
dupe = lambda: [
    {"source_listing_id": "7", "url": "http://x/search",
     "address_raw": "Via Roma 12", "comune": "Bari", "title": "from search"},
    {"source_listing_id": "7", "url": "http://x/detail",
     "address_raw": "Via Roma 12", "comune": "Bari", "title": "from detail"},
]
rep8 = Ingestor(db_dup, source_id=1, backoff_seconds=0).ingest([dupe])
check("duplicate id stored once", rep8.records_stored, 1)
check("detail page wins",
      db_dup.listings[(1, "7")]["title"], "from detail")

# Zero records is a valid outcome and must not be an error.
empty = Ingestor(InMemoryDB(), source_id=1, backoff_seconds=0).ingest([lambda: []])
check("empty page is ok", empty.status, RunStatus.OK)
check("empty page stores nothing", empty.records_stored, 0)
check("cost per 1k is zero when nothing usable", empty.to_dict()["cost_per_1k_usable"], 0)


# ------------------------------------------------------------------ report

print()
if failures:
    print(f"FAILED {len(failures)}/{checks}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED all {checks} checks")