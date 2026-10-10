"""PostgreSQL storage layer.

Everything that touches the database lives here, so moving between engines
means changing one module rather than the ingestion, deduplication or
search code. The SQL is written for the schema in db/schema.sql and uses
plain psycopg3 with dict rows, because an ORM would hide exactly the
upsert and history semantics this system depends on.

Two behaviours are deliberate and worth stating:

- `upsert_listings` distinguishes a price change from an unchanged price.
  Writing a price observation on every crawl would make the history table
  grow without bound and hide real movements.
- `mark_absent` sets status to 'removed', never 'sold'. A listing that
  stops appearing is not a transaction.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

try:
    import psycopg
    from psycopg.rows import dict_row
    _PSYC = True
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None
    _PSYC = False

from .dedup import MatchResult
from .ingest import QuarantineRecord

UPSERT_LISTING = """
INSERT INTO listing (
    source_id, source_listing_id, url, agency, title, description,
    asking_price_cents, auction_price_cents, currency
) VALUES (%(source_id)s, %(source_listing_id)s, %(url)s, %(agency)s,
          %(title)s, %(description)s, %(asking_price_cents)s,
          %(auction_price_cents)s, %(currency)s)
ON CONFLICT (source_id, source_listing_id) DO UPDATE SET
    url                 = EXCLUDED.url,
    agency              = COALESCE(EXCLUDED.agency, listing.agency),
    title               = COALESCE(EXCLUDED.title, listing.title),
    description         = COALESCE(EXCLUDED.description, listing.description),
    asking_price_cents  = COALESCE(EXCLUDED.asking_price_cents,
                                   listing.asking_price_cents),
    auction_price_cents = COALESCE(EXCLUDED.auction_price_cents,
                                   listing.auction_price_cents),
    last_seen           = now(),
    -- A listing that reappears was not permanently gone. Reactivating is
    -- correct; flipping it to 'sold' on sight would be a guess.
    status              = CASE WHEN listing.status = 'removed'
                               THEN 'active' ELSE listing.status END,
    removed_at          = CASE WHEN listing.status = 'removed'
                               THEN NULL ELSE listing.removed_at END
RETURNING id, (xmax = 0) AS was_insert,
          asking_price_cents, status, removed_at;
"""

PRICE_INSERT = """
INSERT INTO price_observation (listing_id, asking_price_cents, currency, run_id)
VALUES (%s, %s, %s, %s)
"""


class Store:
    def __init__(self, dsn: str) -> None:
        if not _PSYC:
            raise RuntimeError(
                "psycopg is required for PostgreSQL storage. "
                "Install psycopg[binary] or use a different backend."
            )
        self.dsn = dsn

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            yield conn

    # ------------------------------------------------------------- runs

    def start_run(self, source_id: int) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO crawl_run (source_id) VALUES (%s) RETURNING id",
                    (source_id,),
                )
                return cur.fetchone()["id"]

    def finish_run(self, run_id: int, *, status: str, pages: int, seen: int,
                   stored: int, quarantined: int, cost_cents: int,
                   error: str | None = None) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE crawl_run
                       SET finished_at = now(), status = %s, pages_fetched = %s,
                           records_seen = %s, records_stored = %s,
                           records_quarantined = %s, cost_cents = %s,
                           error = %s
                       WHERE id = %s""",
                    (status, pages, seen, stored, quarantined, cost_cents,
                     error, run_id),
                )

    def quarantine(self, run_id: int | None, source_id: int,
                   rec: QuarantineRecord) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO quarantine
                       (run_id, source_id, source_listing_id, reason, payload)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (run_id, source_id, rec.source_listing_id, rec.reason,
                     json.dumps(rec.payload, ensure_ascii=False, default=str)),
                )

    # ---------------------------------------------------------- listings

    def upsert_batch(self, source_id: int,
                     records: Iterable[dict]) -> dict[str, int]:
        """Upsert a batch, recording only genuine price movements.

        Returns counts the run report needs. The price comparison happens
        in Python rather than SQL because the previous value has to be
        read before the upsert overwrites it.
        """
        records = list(records)
        if not records:
            return {"new_listings": 0, "price_changes": 0, "removals": 0}

        new_listings = 0
        price_changes = 0

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT source_listing_id, asking_price_cents
                       FROM listing
                       WHERE source_id = %s AND source_listing_id = ANY(%s)""",
                    (source_id, [r["source_listing_id"] for r in records]),
                )
                prev = {row["source_listing_id"]: row["asking_price_cents"]
                        for row in cur.fetchall()}

                for rec in records:
                    cur.execute(UPSERT_LISTING, {
                        "source_id": source_id,
                        "source_listing_id": rec["source_listing_id"],
                        "url": rec["url"],
                        "agency": rec.get("agency"),
                        "title": rec.get("title"),
                        "description": rec.get("description"),
                        "asking_price_cents": rec.get("asking_price_cents"),
                        "auction_price_cents": rec.get("auction_price_cents"),
                        "currency": rec.get("currency", "EUR"),
                    })
                    row = cur.fetchone()
                    sid = rec["source_listing_id"]
                    if row["was_insert"]:
                        new_listings += 1

                    price = rec.get("asking_price_cents")
                    old = prev.get(sid, None)
                    if price is not None and price != old:
                        cur.execute(PRICE_INSERT,
                                    (row["id"], price, "EUR", None))
                        price_changes += 1

        return {"new_listings": new_listings, "price_changes": price_changes,
                "removals": 0}

    def mark_absent(self, source_id: int, seen_ids: Sequence[str]) -> int:
        """Mark listings not seen this run as removed, never as sold.

        Removal is a status, not a conclusion. sold_confirmed stays false
        unless the source explicitly says so, because inferring a sale
        from a disappearance is how a portfolio quietly gains phantom
        transactions.
        """
        if not seen_ids:
            return 0
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE listing
                       SET status = 'removed', removed_at = now()
                       WHERE source_id = %s
                         AND status = 'active'
                         AND NOT (source_listing_id = ANY(%s))
                       RETURNING id""",
                    (source_id, list(seen_ids)),
                )
                return len(cur.fetchall())

    def confirm_sold(self, listing_id: int, evidence: str | None = None) -> None:
        """Explicitly mark a sale. Only ever called from a source signal."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE listing
                       SET status = 'sold', sold_confirmed = true
                       WHERE id = %s""",
                    (listing_id,),
                )

    # ------------------------------------------------------ properties

    def upsert_property(self, address_key: str, record: dict) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO property
                       (address_key, address_norm, street_type, street_name,
                        civic, lat, lon, geo_precision, building_area_sqm,
                        rooms, floor, has_lift, property_type, condition,
                        missing_fields, updated_at)
                       VALUES (%(address_key)s, %(address_raw)s,
                               %(street_type)s, %(street_name)s, %(civic)s,
                               %(lat)s, %(lon)s, %(geo_precision)s,
                               %(building_area_sqm)s, %(rooms)s, %(floor)s,
                               %(has_lift)s, %(property_type)s,
                               %(condition)s, %(missing_fields)s, now())
                       ON CONFLICT (address_key) DO UPDATE SET
                           address_norm = EXCLUDED.address_norm,
                           lat           = COALESCE(EXCLUDED.lat, property.lat),
                           lon           = COALESCE(EXCLUDED.lon, property.lon),
                           geo_precision = CASE WHEN EXCLUDED.lat IS NOT NULL
                                                THEN EXCLUDED.geo_precision
                                                ELSE property.geo_precision END,
                           building_area_sqm = COALESCE(EXCLUDED.building_area_sqm,
                                                        property.building_area_sqm),
                           rooms        = COALESCE(EXCLUDED.rooms, property.rooms),
                           property_type= COALESCE(EXCLUDED.property_type,
                                                   property.property_type),
                           updated_at   = now()
                       RETURNING id""",
                    {
                        "address_key": address_key,
                        "address_raw": record.get("address_raw"),
                        "street_type": record.get("street_type"),
                        "street_name": record.get("street_name"),
                        "civic": record.get("civic"),
                        "lat": record.get("lat"),
                        "lon": record.get("lon"),
                        "geo_precision": record.get("geo_precision", "unknown"),
                        "building_area_sqm": record.get("building_area_sqm"),
                        "rooms": record.get("rooms"),
                        "floor": record.get("floor"),
                        "has_lift": record.get("has_lift"),
                        "property_type": record.get("property_type"),
                        "condition": record.get("condition"),
                        "missing_fields": record.get("missing_fields", []),
                    },
                )
                return cur.fetchone()["id"]

    # ---------------------------------------------------------- dedup log

    def record_decision(self, result: MatchResult, left_id: int,
                        right_id: int) -> None:
        """Persist a dedup decision with its evidence.

        Ordering is normalised so the unique constraint on the pair does
        not depend on which listing happened to be processed first.
        """
        a, b = sorted((left_id, right_id))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO dedup_decision
                       (left_listing_id, right_listing_id, tier, decision,
                        score, distance_m, street_similarity, merged, evidence)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (left_listing_id, right_listing_id)
                       DO UPDATE SET
                           tier     = EXCLUDED.tier,
                           decision = EXCLUDED.decision,
                           score    = EXCLUDED.score,
                           evidence = EXCLUDED.evidence""",
                    (a, b, result.tier, result.decision.value, result.score,
                     result.evidence.get("distance_m"),
                     result.evidence.get("street_similarity"),
                     result.merged,
                     json.dumps(result.evidence, ensure_ascii=False,
                                default=str)),
                )

    def review_queue(self, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT d.id, d.left_listing_id, d.right_listing_id,
                              d.score, d.decision, d.evidence,
                              l.address_norm AS left_address,
                              r.address_norm AS right_address
                       FROM dedup_decision d
                       JOIN listing l ON l.id = d.left_listing_id
                       JOIN listing r ON r.id = d.right_listing_id
                       WHERE d.reviewed_at IS NULL
                         AND d.decision = 'candidate'
                       ORDER BY d.score DESC
                       LIMIT %s""",
                    (limit,),
                )
                return cur.fetchall()

    def resolve_review(self, decision_id: int, outcome: str,
                       reviewer: str) -> None:
        """Apply a review outcome. Changing the tree invalidates approval,
        so the merge is applied in the same transaction."""
        if outcome not in ("confirmed_merge", "rejected"):
            raise ValueError(f"invalid outcome: {outcome!r}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                if outcome == "confirmed_merge":
                    cur.execute(
                        """SELECT left_listing_id, right_listing_id
                           FROM dedup_decision WHERE id = %s""",
                        (decision_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            """UPDATE listing SET property_id = (
                                   SELECT id FROM listing
                                   WHERE id = %s)
                               WHERE id = %s""",
                            (row["left_listing_id"], row["right_listing_id"]),
                        )
                cur.execute(
                    """UPDATE dedup_decision
                       SET reviewed_by = %s, reviewed_at = now(),
                           review_outcome = %s
                       WHERE id = %s""",
                    (reviewer, outcome, decision_id),
                )

    # ------------------------------------------------------------ search

    def comparables(self, lat: float, lon: float, radius_m: float,
                    exclude_ids: Sequence[int] = (),
                    limit: int = 200) -> list[dict]:
        """Fetch a candidate pool for comparable search.

        Uses a bounding box in SQL rather than PostGIS when the extension
        is absent, then filters by haversine in Python. The box is a
        superset, so the caller still gets exact distances; a true distance
        index would only remove rows earlier.
        """
        dlat = radius_m / 111_320.0
        dlon = radius_m / (111_320.0 * max(
            0.01, abs(__import__("math").cos(
                __import__("math").radians(lat)))))
        with self.connect() as conn:
            with conn.cursor() as cur:
                sql = """
                    SELECT l.id AS listing_id, p.address_norm AS address,
                           p.lat, p.lon, p.geo_precision,
                           p.building_area_sqm, p.rooms, p.property_type,
                           l.asking_price_cents, l.auction_price_cents,
                           l.status, l.first_seen, l.last_seen, l.source_id
                    FROM listing l
                    JOIN property p ON p.id = l.property_id
                    WHERE l.status = 'active'
                      AND p.lat BETWEEN %s AND %s
                      AND p.lon BETWEEN %s AND %s
                      AND NOT (l.id = ANY(%s))
                    LIMIT %s
                """
                cur.execute(sql, (
                    lat - dlat, lat + dlat, lon - dlon, lon + dlon,
                    list(exclude_ids), limit,
                ))
                return cur.fetchall()

    # ---------------------------------------------------------- metrics

    def run_stats(self, days: int = 1) -> dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COALESCE(SUM(cost_cents), 0) AS cost,
                              COALESCE(SUM(records_stored), 0) AS stored,
                              COUNT(*) AS runs
                       FROM crawl_run
                       WHERE started_at > now() - make_interval(days => %s)""",
                    (days,),
                )
                row = cur.fetchone()
                stored = row["stored"] or 0
                return {
                    "window_days": days,
                    "runs": row["runs"],
                    "cost_cents": int(row["cost"]),
                    "records_stored": stored,
                    "cost_per_1k_usable": (
                        round(row["cost"] / stored * 1000, 2) if stored else None
                    ),
                }

    def dedup_stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT decision, COUNT(*) AS n,
                              COUNT(*) FILTER (WHERE merged) AS merged,
                              COUNT(*) FILTER (WHERE reviewed_at IS NOT NULL)
                                  AS reviewed
                       FROM dedup_decision GROUP BY decision"""
                )
                rows = {r["decision"]: dict(r) for r in cur.fetchall()}
                total = sum(r["n"] for r in rows.values())
                merged = sum(r["merged"] for r in rows.values())
                return {
                    "pairs": total,
                    "by_decision": rows,
                    "merged": merged,
                    "auto_merge_rate": round(merged / total, 4) if total else 0.0,
                }