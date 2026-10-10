"""Tests for the HTTP layer.

The API is run against an in-memory backend so the response contracts are
exercised without PostgreSQL. What is asserted is mostly about the
caveats: an endpoint that returns numbers without saying where they came
from is the failure this system exists to prevent, so the disclaimer
fields are treated as part of the contract rather than as decoration.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app.api as api
from app.dedup import Listing

failures: list[str] = []
checks = 0


def check(name: str, actual, expected) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{name}\n     expected: {expected!r}\n     actual:   {actual!r}")


def check_true(name: str, value) -> None:
    check(name, bool(value), True)


class FakeCursor:
    """Minimal cursor: execute records the SQL, fetchall returns rows."""

    def __init__(self, rows):
        self._rows = rows
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeBackend:
    """Enough of the storage interface for the API to be exercised."""

    def __init__(self) -> None:
        self.pool = [
            {"listing_id": 1, "address": "Via Roma 12", "lat": 41.11710,
             "lon": 16.87190, "geo_precision": "exact", "building_area_sqm": 85,
             "rooms": 3, "property_type": "flat", "asking_price_cents": 150000,
             "auction_price_cents": None, "status": "active", "comune": "Bari",
             "last_seen": "2026-10-01", "first_seen": "2026-09-01"},
            {"listing_id": 2, "address": "Via Roma 14", "lat": 41.11750,
             "lon": 16.87190, "geo_precision": "approx", "building_area_sqm": 180,
             "rooms": 5, "property_type": "flat", "asking_price_cents": None,
             "auction_price_cents": 90000, "status": "active", "comune": "Bari",
             "last_seen": "2026-10-01", "first_seen": "2026-09-15"},
        ]
        self.rows = [
            {"listing_id": 1, "address": "Via Roma 12",
             "building_area_sqm": 85, "rooms": 3, "property_type": "flat",
             "geo_precision": "exact", "asking_price_cents": 150000,
             "auction_price_cents": None, "status": "active",
             "last_seen": "2026-10-01", "first_seen": "2026-09-01",
             "source_id": 1, "comune": "Bari", "lat": 41.1, "lon": 16.9,
             "address_raw": "Via Roma 12"},
        ]

    def comparables(self, lat, lon, radius_m, exclude_ids=(), limit=200):
        return self.pool

    def review_queue(self, limit=50):
        return []

    def dedup_stats(self):
        return {"pairs": 10, "merged": 6, "auto_merge_rate": 0.6,
                "by_decision": {"exact": 1, "geo": 5, "candidate": 4}}

    def run_stats(self, days=1):
        return {"window_days": days, "runs": 2, "cost_cents": 500,
                "records_stored": 2, "cost_per_1k_usable": 250000.0}

    def connect(self):
        backend = self

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return FakeCursor(backend.rows)

        return _Ctx()


api._backend = FakeBackend()
client = TestClient(api.app)

# ---------------------------------------------------------------- health

h = client.get("/health")
check("health ok", h.status_code, 200)
check("health status", h.json()["status"], "ok")
check("radii start at 50m", h.json()["default_radii_m"][0], 50.0)

# ----------------------------------------------------------- comparables

r = client.post("/comparables", json={"lat": 41.1171, "lon": 16.8719})
check("comparables ok", r.status_code, 200)
b = r.json()
check_true("comparables found", b["total_found"] > 0)
check_true("warning always present", bool(b.get("warning")))
check_true("warning disclaims valuation", "not a valuation" in b["warning"])
check_true("radius attribution present",
           all("found_at_radius_m" in x for x in b["results"]))
check_true("confidence present",
           all("confidence" in x for x in b["results"]))
check_true("price type declared",
           all("price_source" in x for x in b["results"]))
check("quality flag present", "comparable_set_quality" in b, True)

# Auction rows must not be presented as asking prices.
by_id = {x["listing_id"]: x for x in b["results"]}
if 2 in by_id:
    check("auction labelled", by_id[2]["price_source"], "auction")
    check("auction has no asking price", by_id[2]["asking_price_cents"], None)

# Exclusions must be honoured.
r2 = client.post("/comparables",
                 json={"lat": 41.1171, "lon": 16.8719, "exclude_ids": [1, 2]})
check("exclusions honoured", r2.json()["total_found"], 0)

# Validation must reject nonsense coordinates.
check("latitude out of range rejected",
      client.post("/comparables", json={"lat": 999, "lon": 0}).status_code, 422)
check("empty body rejected",
      client.post("/comparables", json={}).status_code, 422)
check("max_results bounded",
      client.post("/comparables",
                  json={"lat": 41.1, "lon": 16.8, "max_results": 0}).status_code, 422)

# ---------------------------------------------------------------- search

s = client.post("/search", json={"comune": "Bari", "min_area": 50})
check("search ok", s.status_code, 200)
sb = s.json()
check("price filter scope declared",
      sb["price_filter_applies_to"], "asking_price_cents")
check_true("auctions excluded from price filters",
           "Auction valuations are excluded" in sb["note"])

check("search rejects negative area",
      client.post("/search", json={"min_area": -1}).status_code, 422)
check("search limit bounded",
      client.post("/search", json={"limit": 9999}).status_code, 422)

# ------------------------------------------------------------- dedup api

d = client.post("/dedup/evaluate", json=[
    {"id": "a", "address_raw": "Via Giuseppe Verdi 12", "comune": "Bari",
     "lat": 41.1171, "lon": 16.8719, "area_sqm": 85, "rooms": 3},
    {"id": "b", "address_raw": "V. G. Verdi, 12", "comune": "Bari",
     "lat": 41.11712, "lon": 16.8719, "area_sqm": 87, "rooms": 3},
    {"id": "c", "address_raw": "Verdi 12", "comune": "Bari",
     "lat": 41.11715, "lon": 16.87192, "area_sqm": 300, "rooms": 8},
])
check("dedup evaluate ok", d.status_code, 200)
db_ = d.json()
check_true("summary returned", "summary" in db_)
check_true("decisions carry evidence",
           all("evidence" in x for x in db_["decisions"]))
check_true("audit requirement stated",
           "audit sample" in db_["note"])

st = client.get("/dedup/stats")
check("stats ok", st.status_code, 200)
check_true("auto merge rate reported", "auto_merge_rate" in st.json())

q = client.get("/dedup/review-queue")
check("review queue ok", q.status_code, 200)
check("review queue shape", "pending" in q.json(), True)

# -------------------------------------------------------------- metrics

m = client.get("/metrics/run?days=7")
check("run metrics ok", m.status_code, 200)
check("window echoed", m.json()["window_days"], 7)
check_true("cost per 1k present", "cost_per_1k_usable" in m.json())

cm = client.get("/metrics/completeness")
check("completeness ok", cm.status_code, 200)
check_true("completeness per field",
           "building_area_sqm" in cm.json()["completeness"])
check_true("coverage present", "per_comune" in cm.json()["coverage"])

# --------------------------------------------------- degraded mode honesty

api._backend = None
api.DSN = ""
db_fail = client.post("/comparables", json={"lat": 41.1, "lon": 16.8})
check("no database reports 503", db_fail.status_code, 503)
check_true("503 explains what is missing",
           "DATABASE_URL" in db_fail.json()["detail"])
check("search also 503",
      client.post("/search", json={}).status_code, 503)

# Health must not pretend a database exists when it does not.
h2 = client.get("/health")
check("health admits no database", h2.json()["database"], "not configured")

api._backend = FakeBackend()


# ------------------------------------------------------------------ report

print()
if failures:
    print(f"FAILED {len(failures)}/{checks}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED all {checks} checks")