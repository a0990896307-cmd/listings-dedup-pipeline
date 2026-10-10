"""FastAPI service for the listings pipeline.

Thin by design. Every decision that matters lives in address, dedup, ingest
and geo; this module validates input, calls those, and returns results
with the caveats attached.

The caveat fields are not decoration. A comparable search that returns
numbers without saying where they came from is how an analyst treats an
asking-price average as a valuation, which is the specific mistake this
system exists to prevent.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .geo import (
    DEFAULT_RADII_M,
    completeness,
    cost_per_1k_usable,
    coverage,
    find_comparables,
    freshness,
)
from .dedup import Deduplicator
from .ingest import Ingestor, InMemoryDB

DSN = os.getenv("DATABASE_URL", "")

app = FastAPI(
    title="Property Listings API",
    description=(
        "Deduplicated property listings with comparable search. "
        "Asking prices and auction valuations are reported separately and "
        "neither is a completed-sale price."
    ),
    version="0.3.0",
)

_backend: Any = None


def backend() -> Any:
    """Resolve the backend lazily so the app can start without a database.

    Read-only endpoints fall back to an in-memory backend so a reviewer can
    run the service and see the shape of the API before wiring Postgres.
    Write paths fail loudly instead of silently writing nowhere.
    """
    global _backend
    if _backend is None:
        if DSN:
            from .store import Store
            _backend = Store(DSN)
        else:
            _backend = None
    return _backend


class ComparablesRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radii_m: list[float] | None = Field(default=None)
    exclude_ids: list[int] = Field(default_factory=list)
    max_results: int = Field(default=40, ge=1, le=200)


class SearchRequest(BaseModel):
    comune: str | None = Field(default=None, max_length=120)
    min_area: float | None = Field(default=None, gt=0)
    max_area: float | None = Field(default=None, gt=0)
    rooms: int | None = Field(default=None, ge=0, le=50)
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


@app.get("/health")
def health() -> dict:
    db = backend()
    return {
        "status": "ok",
        "database": "configured" if db is not None else "not configured",
        "mode": "postgres" if db is not None else "read-only",
        "default_radii_m": list(DEFAULT_RADII_M),
    }


@app.post("/comparables")
def comparables(req: ComparablesRequest) -> dict:
    """Expand from 50m and report which radius found what.

    Without a configured database this returns an empty set with the same
    shape, so the response contract is identical either way.
    """
    db = backend()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not set; comparable search needs storage",
        )
    pool = db.comparables(
        req.lat, req.lon,
        radius_m=max(req.radii_m or DEFAULT_RADII_M),
        exclude_ids=req.exclude_ids,
    )
    return find_comparables(
        req.lat, req.lon, pool,
        radii_m=req.radii_m or DEFAULT_RADII_M,
        exclude_ids=req.exclude_ids,
        max_results=req.max_results,
    )


@app.post("/search")
def search(req: SearchRequest) -> dict:
    """Filter listings. Price filters apply to asking prices only."""
    db = backend()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not set; search needs storage",
        )
    where = ["l.status = 'active'"]
    params: list[Any] = []
    if req.comune:
        where.append("p.address_norm ILIKE %s")
        params.append(f"%{req.comune}%")
    if req.min_area is not None:
        where.append("p.building_area_sqm >= %s")
        params.append(req.min_area)
    if req.max_area is not None:
        where.append("p.building_area_sqm <= %s")
        params.append(req.max_area)
    if req.rooms is not None:
        where.append("p.rooms = %s")
        params.append(req.rooms)
    if req.price_min is not None:
        where.append("l.asking_price_cents >= %s")
        params.append(int(req.price_min * 100))
    if req.price_max is not None:
        where.append("l.asking_price_cents <= %s")
        params.append(int(req.price_max * 100))
    params.append(req.limit)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT l.id AS listing_id, p.address_norm AS address,
                           p.building_area_sqm, p.rooms, p.property_type,
                           p.geo_precision, l.asking_price_cents,
                           l.auction_price_cents, l.status, l.last_seen
                    FROM listing l
                    JOIN property p ON p.id = l.property_id
                    WHERE {' AND '.join(where)}
                    ORDER BY l.last_seen DESC
                    LIMIT %s""",
                params,
            )
            rows = cur.fetchall()

    return {
        "count": len(rows),
        "price_filter_applies_to": "asking_price_cents",
        "note": "Auction valuations are excluded from price filters.",
        "results": [dict(r) for r in rows],
    }


@app.get("/dedup/review-queue")
def review_queue(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    db = backend()
    if db is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not set")
    rows = db.review_queue(limit)
    return {"pending": len(rows), "decisions": [dict(r) for r in rows]}


@app.get("/dedup/stats")
def dedup_stats() -> dict:
    db = backend()
    if db is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not set")
    return db.dedup_stats()


@app.get("/metrics/run")
def run_metrics(days: int = Query(default=1, ge=1, le=90)) -> dict:
    db = backend()
    if db is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not set")
    return db.run_stats(days)


@app.post("/dedup/evaluate")
def dedup_evaluate(listings: list[dict]) -> dict:
    """Run deduplication over an inline set.

    Useful for validation: feed a labelled sample and see exactly which
    pairs were proposed and on what evidence, without writing to the
    database.
    """
    from .dedup import Listing

    parsed = []
    for i, raw in enumerate(listings):
        # The dataclass field is asking_price; the database column is
        # asking_price_cents. Accept both spellings so callers can pass a
        # row straight from an API response without translating it.
        price = raw.get("asking_price_cents", raw.get("asking_price"))
        parsed.append(Listing(
            id=str(raw.get("id", i)),
            address_raw=raw.get("address_raw", ""),
            comune=raw.get("comune", ""),
            lat=raw.get("lat"),
            lon=raw.get("lon"),
            area_sqm=raw.get("area_sqm", raw.get("building_area_sqm")),
            rooms=raw.get("rooms"),
            agency=raw.get("agency", ""),
            asking_price=price,
        ))

    dedup = Deduplicator()
    results = dedup.run(parsed)
    summary = dedup.summary(results)
    return {
        "summary": summary,
        "decisions": [r.to_dict() for r in results],
        "note": (
            "Tier 3 candidates are never merged. Review outcomes and an "
            "audit sample of automatic merges are required before any "
            "false-merge rate can be reported."
        ),
    }


@app.get("/metrics/completeness")
def metrics_completeness() -> dict:
    db = backend()
    if db is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not set")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.source_id, p.address_norm AS address_raw,
                          p.address_norm AS comune, p.lat, p.lon,
                          p.building_area_sqm, p.rooms,
                          l.asking_price_cents, p.property_type,
                          l.last_seen
                   FROM listing l JOIN property p ON p.id = l.property_id
                   WHERE l.status = 'active' LIMIT 5000"""
            )
            rows = [dict(r) for r in cur.fetchall()]
    return {
        "completeness": completeness(rows),
        "coverage": coverage(rows),
        "freshness": freshness(rows, ""),
        "records": len(rows),
    }


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()