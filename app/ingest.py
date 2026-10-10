"""Ingestion with retries, quarantine and idempotent upserts.

The failure modes this exists to handle are the ones that silently corrupt
a listings database:

- a source schema change that starts dropping rows without erroring
- a partial page fetch that looks like a quiet market
- a re-run that duplicates instead of updating
- an attribute disappearing and being written as an empty string, which is
  indistinguishable from "the property has no balcony"

Every fetch is recorded in `crawl_run`, unexpected records go to
`quarantine` rather than being discarded, and upserts are keyed on the
source identity so re-running is safe.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

# Attributes the pilot depends on. A record missing the required ones
# cannot be deduplicated or geocoded, so it is quarantined with a reason
# rather than stored as an unusable row.
REQUIRED = ("source_listing_id", "url", "address_raw")

# Attributes recorded when present. Absence is tracked in missing_fields.
OPTIONAL = (
    "comune", "lat", "lon", "geo_precision", "area_sqm", "rooms",
    "floor", "has_lift", "property_type", "condition", "agency",
    "asking_price_cents", "auction_price_cents", "title", "description",
)

# Values that mean "absent" and must not be written as data. Empty string
# is the trap here: '' is not NULL, and a filter for "has balcony" would
# treat it as present-but-false.
EMPTY_VALUES = {"", " ", "-", "n/a", "na", "null", "none", "undefined"}


class RunStatus:
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class QuarantineRecord:
    reason: str
    payload: dict
    source_listing_id: str | None = None


@dataclass
class RunReport:
    run_id: int | None = None
    pages_fetched: int = 0
    records_seen: int = 0
    records_stored: int = 0
    records_quarantined: int = 0
    new_properties: int = 0
    price_changes: int = 0
    removals: int = 0
    cost_cents: int = 0
    status: str = RunStatus.RUNNING
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "pages_fetched": self.pages_fetched,
            "records_seen": self.records_seen,
            "records_stored": self.records_stored,
            "records_quarantined": self.records_quarantined,
            "new_properties": self.new_properties,
            "price_changes": self.price_changes,
            "removals": self.removals,
            "cost_cents": self.cost_cents,
            "usable_records": self.records_stored,
            "cost_per_1k_usable": (
                round(self.cost_cents / self.records_stored * 1000)
                if self.records_stored else 0
            ),
            "errors": self.errors,
        }


def clean_value(v: Any) -> Any | None:
    """Normalise a single field value, or None when it means absent."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in EMPTY_VALUES:
            return None
        return s
    if isinstance(v, (int, float)):
        return v
    return v


def normalise_record(raw: dict, source_id: int) -> tuple[dict | None, str | None]:
    """Validate and clean one raw record.

    Returns (clean_record, None) or (None, reason). The reason is stored
    with the quarantined payload so a schema change can be diagnosed from
    the quarantine table alone.
    """
    missing_required = [k for k in REQUIRED if not raw.get(k)]
    if missing_required:
        return None, f"missing required: {','.join(missing_required)}"

    clean: dict[str, Any] = {"source_id": source_id}
    absent: list[str] = []

    for key in ("source_listing_id", "url", "address_raw"):
        clean[key] = clean_value(raw.get(key))
        if clean[key] is None:
            return None, f"empty required field: {key}"

    for key in OPTIONAL:
        v = clean_value(raw.get(key))
        if v is None:
            absent.append(key)
        else:
            clean[key] = v

    # Coordinates are only usable as a pair. Half a coordinate is worse
    # than none, because it silently produces wrong distances.
    if clean.get("lat") is None or clean.get("lon") is None:
        if clean.get("lat") is not None or clean.get("lon") is not None:
            return None, "coordinate pair incomplete"
        clean["lat"] = None
        clean["lon"] = None
        clean["geo_precision"] = "unknown"

    # Never invent a precision flag. If the source did not state one, the
    # coordinate is 'unknown', not 'exact'.
    gp = clean.get("geo_precision")
    if gp not in ("exact", "approx", "unknown"):
        clean["geo_precision"] = "unknown"

    clean["missing_fields"] = sorted(absent)
    return clean, None


class Ingestor:
    """Orchestrates a fetch, validate, upsert cycle.

    The database is injected rather than constructed here so the logic can
    be tested against a fake without standing up PostgreSQL.
    """

    def __init__(self, db, source_id: int, max_retries: int = 3,
                 backoff_seconds: float = 0.5) -> None:
        self.db = db
        self.source_id = source_id
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def _fetch_with_retry(self, fetcher: Callable[[], Iterable[dict]],
                          report: RunReport) -> list[dict] | None:
        """Retry a page fetch, recording each failure.

        Retries are bounded. An unbounded retry against a rate-limited or
        blocked source is how a crawler becomes a problem for the source
        rather than for itself.
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                records = list(fetcher())
                report.pages_fetched += 1
                return records
            except Exception as exc:  # noqa: BLE001 - surfaced in the report
                msg = f"attempt {attempt}/{self.max_retries}: {type(exc).__name__}: {exc}"
                report.errors.append(msg)
                if attempt < self.max_retries:
                    # Linear backoff. Exponential would be more polite, but
                    # the failure here is usually a schema error rather
                    # than a rate limit, where waiting does not help.
                    time.sleep(self.backoff_seconds * attempt)
        return None

    def ingest(self, pages: Sequence[Callable[[], Iterable[dict]]],
               cost_cents: int = 0) -> RunReport:
        report = RunReport(cost_cents=cost_cents)
        run_id = self.db.start_run(self.source_id)
        report.run_id = run_id

        collected: dict[str, dict] = {}
        for page in pages:
            records = self._fetch_with_retry(page, report)
            if records is None:
                continue
            for raw in records:
                report.records_seen += 1
                clean, reason = normalise_record(raw, self.source_id)
                if clean is None:
                    report.records_quarantined += 1
                    self.db.quarantine(
                        run_id, self.source_id,
                        QuarantineRecord(reason=reason, payload=raw),
                    )
                    continue
                # Later pages win for a duplicated source id, which is the
                # correct behaviour when a listing appears in both a search
                # result and a detail page.
                collected[clean["source_listing_id"]] = clean

        report.records_stored = len(collected)

        try:
            outcome = self.db.upsert_batch(self.source_id, collected.values())
            report.new_properties = outcome.get("new_properties", 0)
            report.price_changes = outcome.get("price_changes", 0)
            report.removals = outcome.get("removals", 0)
            report.status = RunStatus.OK if not report.errors else RunStatus.PARTIAL
        except Exception as exc:  # noqa: BLE001
            report.status = RunStatus.FAILED
            report.errors.append(f"upsert failed: {type(exc).__name__}: {exc}")

        self.db.finish_run(
            run_id,
            status=report.status,
            pages=report.pages_fetched,
            seen=report.records_seen,
            stored=report.records_stored,
            quarantined=report.records_quarantined,
            cost_cents=report.cost_cents,
            error="; ".join(report.errors)[:2000] or None,
        )
        return report


class InMemoryDB:
    """Fake used by the tests to exercise Ingestor without PostgreSQL."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.quarantine_rows: list[dict] = []
        self.listings: dict[tuple[int, str], dict] = {}
        self.properties: dict[str, dict] = {}
        self.price_history: list[dict] = []

    def start_run(self, source_id: int) -> int:
        rid = len(self.runs) + 1
        self.runs.append({"id": rid, "source_id": source_id, "status": "running"})
        return rid

    def finish_run(self, run_id, **kw) -> None:
        for r in self.runs:
            if r["id"] == run_id:
                r.update(kw)

    def quarantine(self, run_id, source_id, rec: QuarantineRecord) -> None:
        self.quarantine_rows.append({
            "run_id": run_id,
            "source_id": source_id,
            "reason": rec.reason,
            "payload": rec.payload,
        })

    def upsert_batch(self, source_id: int, records: Iterable[dict]) -> dict:
        new_properties = 0
        price_changes = 0

        from app.address import normalise as norm_addr

        for rec in records:
            sid = rec["source_listing_id"]
            key = (source_id, sid)
            addr = norm_addr(rec.get("address_raw", ""), rec.get("comune", ""))
            akey = addr.key

            if akey and akey not in self.properties:
                self.properties[akey] = {
                    "address_key": akey,
                    "address_raw": rec.get("address_raw"),
                    "comune": rec.get("comune"),
                    "missing_fields": rec.get("missing_fields", []),
                    "lat": rec.get("lat"),
                    "lon": rec.get("lon"),
                }
                new_properties += 1

            prev = self.listings.get(key)
            price = rec.get("asking_price_cents")
            if prev is None:
                # _last_price must be set here as well as on update, or the
                # next run sees a difference against None and records a
                # price change for a listing that never changed price.
                self.listings[key] = dict(
                    rec, address_key=akey, status="active",
                    _last_price=price,
                )
                if price is not None:
                    self.price_history.append({
                        "source_id": source_id,
                        "source_listing_id": sid,
                        "asking_price_cents": price,
                    })
            else:
                prev.update(rec)
                prev["address_key"] = akey
                prev["last_seen"] = "now"
                if price is not None and prev.get("_last_price") != price:
                    price_changes += 1
                    self.price_history.append({
                        "source_id": source_id,
                        "source_listing_id": sid,
                        "asking_price_cents": price,
                    })
                prev["_last_price"] = price

        return {"new_properties": new_properties, "price_changes": price_changes,
                "removals": 0}