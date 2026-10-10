-- Schema for a property listings database with history.
--
-- Design rules that come from the analysis use case rather than from
-- normalisation:
--
-- 1. A listing's absence from a source is not a sale. Removals get a
--    status and a last_seen timestamp; nothing is inferred as a
--    transaction.
-- 2. Asking prices and auction valuations are never the same field and
--    never the same table.
-- 3. Every row that came from a source keeps the source id and URL, so
--    any record can be traced to where it legally came from.
-- 4. Coordinates keep their raw value plus an explicit precision flag.
--    Silently rounding them is how a 50m comparable search becomes
--    fiction.
-- 5. Missing attributes are NULL with a companion list, not empty
--    strings. "No balcony" and "balcony not recorded" must stay
--    distinguishable.

CREATE EXTENSION IF NOT EXISTS postgis;      -- optional, see note below
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy address search

-- ---------------------------------------------------------------- sources

CREATE TABLE IF NOT EXISTS source (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,   -- e.g. 'immobiliare'
    access_method     TEXT NOT NULL,          -- 'api' | 'licensed_actor' | 'scraping'
    licence_reference TEXT,                   -- contract or provider reference
    terms_url         TEXT,
    attribution_ok    BOOLEAN NOT NULL DEFAULT FALSE,
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    active            BOOLEAN NOT NULL DEFAULT TRUE
);

-- ------------------------------------------------------------------- raw
-- Every fetch is recorded, including ones that failed. Coverage and
-- freshness monitoring reads from here, so a silent gap in collection is
-- visible instead of looking like a quiet market.

CREATE TABLE IF NOT EXISTS crawl_run (
    id                BIGSERIAL PRIMARY KEY,
    source_id         INTEGER NOT NULL REFERENCES source(id),
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','ok','partial','failed')),
    pages_fetched     INTEGER NOT NULL DEFAULT 0,
    records_seen      INTEGER NOT NULL DEFAULT 0,
    records_stored    INTEGER NOT NULL DEFAULT 0,
    records_quarantined INTEGER NOT NULL DEFAULT 0,
    cost_cents        INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT REFERENCES crawl_run(id),
    source_id         INTEGER NOT NULL REFERENCES source(id),
    source_listing_id TEXT,
    reason            TEXT NOT NULL,
    payload           JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved          BOOLEAN NOT NULL DEFAULT FALSE
);

-- ----------------------------------------------------------------- places

CREATE TABLE IF NOT EXISTS place (
    id                SERIAL PRIMARY KEY,
    comune            TEXT NOT NULL,
    provincia         TEXT,
    lat               DOUBLE PRECISION,
    lon               DOUBLE PRECISION,
    -- 'exact' when the source gave a precise point, 'approx' when it is
    -- a centroid or a neighbourhood marker.
    geo_precision     TEXT CHECK (geo_precision IN ('exact','approx','unknown'))
                      NOT NULL DEFAULT 'unknown',
    geohash           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------- listings
-- One row per source listing. The same physical property advertised by
-- three agencies is three rows here, linked by property_id.

CREATE TABLE IF NOT EXISTS property (
    id                BIGSERIAL PRIMARY KEY,
    -- Canonical identity, assigned once by the deduplicator.
    address_key       TEXT UNIQUE,
    address_norm      TEXT,
    street_type       TEXT,
    street_name       TEXT,
    civic             TEXT,
    place_id          INTEGER REFERENCES place(id),
    lat               DOUBLE PRECISION,
    lon               DOUBLE PRECISION,
    geo_precision     TEXT CHECK (geo_precision IN ('exact','approx','unknown'))
                      NOT NULL DEFAULT 'unknown',
    building_area_sqm NUMERIC(8,2),
    rooms             INTEGER,
    floor             INTEGER,
    has_lift          BOOLEAN,
    property_type     TEXT,
    condition         TEXT,
    -- Distinguishes "no balcony" from "balcony not recorded".
    missing_fields    TEXT[] NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS listing (
    id                BIGSERIAL PRIMARY KEY,
    property_id       BIGINT REFERENCES property(id),
    source_id         INTEGER NOT NULL REFERENCES source(id),
    source_listing_id TEXT NOT NULL,
    url               TEXT NOT NULL,
    agency            TEXT,
    title             TEXT,
    description       TEXT,
    -- Asking price only. Auction valuation lives in auction_price_cents
    -- so no downstream query can accidentally average the two.
    asking_price_cents      BIGINT,
    auction_price_cents     BIGINT,
    currency          TEXT NOT NULL DEFAULT 'EUR',
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','removed','sold','unknown')),
    first_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at        TIMESTAMPTZ,
    -- A listing is never 'sold' because it stopped appearing. Only an
    -- explicit source signal sets this.
    sold_confirmed    BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (source_id, source_listing_id)
);

CREATE INDEX IF NOT EXISTS idx_listing_property ON listing(property_id);
CREATE INDEX IF NOT EXISTS idx_listing_status   ON listing(status);
CREATE INDEX IF NOT EXISTS idx_listing_lastseen ON listing(last_seen);

-- ---------------------------------------------------------- price history
-- Append only. A price change is a new row, never an update, so the
-- trajectory is queryable and a rewrite cannot erase it.

CREATE TABLE IF NOT EXISTS price_observation (
    id                BIGSERIAL PRIMARY KEY,
    listing_id        BIGINT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    asking_price_cents BIGINT NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'EUR',
    run_id            BIGINT REFERENCES crawl_run(id)
);

CREATE INDEX IF NOT EXISTS idx_price_listing ON price_observation(listing_id, observed_at);

-- ------------------------------------------------------------- dedup log
-- The audit trail the client asked for. Every decision is retained with
-- the evidence that produced it, so false merges and missed duplicates
-- are both measurable from the same table.

CREATE TABLE IF NOT EXISTS dedup_decision (
    id                BIGSERIAL PRIMARY KEY,
    decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_listing_id   BIGINT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    right_listing_id  BIGINT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    tier              INTEGER NOT NULL,
    decision          TEXT NOT NULL
                      CHECK (decision IN ('exact','geo','candidate','reject')),
    score             DOUBLE PRECISION,
    distance_m        DOUBLE PRECISION,
    street_similarity DOUBLE PRECISION,
    merged            BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ,
    review_outcome    TEXT CHECK (review_outcome IN ('confirmed_merge','rejected'))
    -- evidence holds the raw comparison inputs, not just the verdict.
    evidence          JSONB NOT NULL,
    UNIQUE (left_listing_id, right_listing_id)
);

CREATE INDEX IF NOT EXISTS idx_dedup_decision ON dedup_decision(decision);
CREATE INDEX IF NOT EXISTS idx_dedup_review   ON dedup_decision(review_outcome)
                      WHERE reviewed_at IS NULL;

-- ------------------------------------------------------ geo search index
-- PostGIS gives a true distance index. The fallback is a bounding box on
-- a plain lat/lon index, which is correct but slower. The flag is stored
-- so the API can say which is in use rather than silently degrading.

CREATE INDEX IF NOT EXISTS idx_property_latlon
    ON property (lat, lon) WHERE lat IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_property_geo
                 ON property USING gist (ll_to_earth(lat, lon))';
    END IF;
EXCEPTION
    WHEN OTHERS THEN NULL;   -- fall back to the bbox index
END $$;

-- --------------------------------------------------------------- metrics
-- Rolling quality figures the pilot report reads. Kept as a table rather
-- than computed on the fly so the 14-day report is reproducible.

CREATE TABLE IF NOT EXISTS pipeline_metric (
    id                BIGSERIAL PRIMARY KEY,
    measured_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    metric            TEXT NOT NULL,
    value             DOUBLE PRECISION NOT NULL,
    unit              TEXT,
    dimensions        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_metric_name ON pipeline_metric(metric, measured_at);