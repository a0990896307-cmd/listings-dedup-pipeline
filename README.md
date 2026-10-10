# Property Listings Pipeline — Bari pilot

Ingestion, deduplication and comparable search for a property listings
database. Built so that the two ways this kind of tool quietly corrupts a
dataset — merging two different properties, and inventing a sale that
never happened — are both visible in the data rather than hidden in an
average.

207 checks, no test framework required:

```bash
pip install rapidfuzz unidecode
python tests/test_dedup.py     # 66 checks
python tests/test_ingest.py    # 60 checks
python tests/test_geo.py       # 41 checks
python tests/test_api.py       # 40 checks
```

Every suite exits non-zero on failure and needs no database, so it can be
run before reading anything else.

```bash
python demo.py     # end-to-end, no database or API key
```

## The two errors that matter

**A false merge removes a real property from the comparable set.** Four
things merge as the same listing and the analyst compares a target against
itself.

**A missed duplicate inflates counts** and understates how long a property
has been on the market.

Those are not symmetric, and optimising one number makes the other worse.
So nothing is merged automatically above tier 2, and every decision is
retained with the evidence that produced it.

```
Immobiliare  "Via Giuseppe Verdi 12"  ┐
Idealista    "V. G. Verdi, 12"         ├─→ one property
Subito       "VIA VERDI 12"            │
Trovit       "Via Verdi n. 12"         ┘
GreenHome    "Verdi 12"   300 m²  8 rm  ─→ review (attribute conflict)
```

## Deduplication

Three tiers, each stricter than the last.

| Tier | Basis | Outcome |
|---|---|---|
| 1 | normalised address key, both usable | merged |
| 2 | coordinates within 40 m, attributes agree | merged, evidence recorded |
| 3 | fuzzy similarity above threshold | **review only, never merged** |

Address normalisation handles how Italian agencies actually write the same
address:

```
"Via Giuseppe Verdi 12"  ┐
"Via Roma civ. 12"        ├─→ same key
"V. Roma 12"              │
"Roma 12"                 ┘
```

Accents folded, case ignored, street types canonicalised, `civ.` / `n.`
markers consumed, prepositions dropped, single-letter initials matched
against the name they abbreviate. Street type is deliberately excluded
from the exact key, or `Roma 12` and `Via Roma 12` would split.

**An explicit attribute disagreement vetoes a merge at every tier.** A
300 m² eight-room unit and an 85 m² three-room unit share a civic number
and sit metres apart; merging them on proximity alone destroys a real
property. Only missing data is tolerated. Known-bad data disqualifies.

**Different civic numbers never auto-merge.** Civic 12 and 12A are
different units in one building, and proximity plus similar attributes is
exactly what you would expect between them.

Comparison is blocked by comune and civic number, since a true duplicate
always shares one. A block larger than the limit is reported rather than
compared, because that size is the signature of a parse bug, not a
building.

## Measurements that are honest about themselves

Two populations, reported separately because they come from different
evidence:

| Figure | Source | Meaning |
|---|---|---|
| `false_merge_rate` | audit sample of auto-merges | how often automatic merging is wrong |
| `candidate_precision` | review queue outcomes | how good the candidate ranking is |
| `auto_merge_rate` | decision log | how much was merged without review |
| `missed_duplicate_rate` | labelled negative sample | requires the validation phase |

`missed_duplicate_rate` returns `None` from the decision log on purpose.
A pair that was never compared leaves no row, so any figure derived from
the log alone would be a guess wearing a number. The validation phase
produces a held-back sample of known duplicates for exactly this.

## Comparable search

Expands outward from 50 m and reports which radius captured each result:

```
found_per_radius_m: {"50.0": 2, "100.0": 1, "200.0": 0, "400.0": 2, "800.0": 1}
```

Each result carries `found_at_radius_m`, `geo_precision` and a
`confidence` label. The set as a whole carries a
`comparable_set_quality` flag, because a comparable set is only as good as
its weakest coordinate.

Coordinates keep their raw value plus an explicit `exact` / `approx` /
`unknown` precision flag. Precision is never invented: if the source did
not state one, it is `unknown`.

**Asking prices and auction valuations are separate fields and are never
averaged together.** Every comparable declares which kind of number it
carries, and an auction result inside 100 m has its confidence capped,
because an auction valuation is not a market observation.

The response always includes the disclaimer that this is a comparable set
of asking prices, not a valuation.

## Ingestion

- Bounded retries, linear backoff, every failure recorded in the run log.
  A run that recovered on retry is `partial`, not `ok` — the fact that it
  needed retries should not disappear behind a success.
- Unexpected records go to `quarantine` with a reason and their payload,
  so a schema change is diagnosable from that table alone.
- Upserts are keyed on source identity, so re-running is safe.
- Duplicated source ids within a run resolve to the later record.
- Half a coordinate pair is refused rather than stored, because it
  produces silently wrong distances.
- **Empty string is not data.** `''`, `-`, `n/a` and friends normalise to
  NULL and are recorded in `missing_fields`, so `no balcony` stays
  distinguishable from `balcony not recorded`.

## Schema

`db/schema.sql`. Points worth noting:

- `price_observation` is append-only, so price history cannot be
  rewritten.
- `listing.sold_confirmed` is separate from `status`. A listing that stops
  appearing gets `removed` with a `last_seen` timestamp. Nothing infers a
  transaction from absence.
- `dedup_decision` retains the evidence JSON and the review outcome, and
  has a partial index over unreviewed rows so the queue stays cheap.
- PostGIS index when available, plain bounding box otherwise, with a flag
  so the API reports which is in use instead of degrading silently.

## Layout

```
app/address.py   Italian address normalisation
app/dedup.py     three-tier comparison, blocking, decision log
app/ingest.py    retries, quarantine, idempotent upserts, price history
app/geo.py       comparable search, confidence, quality metrics
db/schema.sql    PostgreSQL schema
```

## API

```bash
uvicorn app.api:app --port 8000
```

| | |
|---|---|
| `GET /health` | mode and whether storage is configured |
| `POST /comparables` | radius-attributed comparable search |
| `POST /search` | filter listings; price filters are asking prices only |
| `GET /dedup/review-queue` | unresolved candidates, highest score first |
| `GET /dedup/stats` | decision counts and auto-merge rate |
| `POST /dedup/evaluate` | run dedup over an inline set without writing |
| `GET /metrics/run` | cost per 1,000 usable over a window |
| `GET /metrics/completeness` | per-field completeness, coverage, freshness |

Endpoints that need storage return 503 with a message naming the missing
variable rather than pretending to have data. `GET /health` reports
`not configured` in that state instead of implying a working backend.

## Running against PostgreSQL

```bash
psql "$DATABASE_URL" -f db/schema.sql
export DATABASE_URL=postgresql://...
uvicorn app.api:app
```

## Not yet built

The 14-day validation harness, and a positive/negative labelled sample for
measuring missed duplicates. Both need real Bari data over time rather
than fixtures.

## Licence

MIT.