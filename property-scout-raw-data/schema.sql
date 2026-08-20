-- Property Values Database schema
-- Run once: psql -h localhost -p 5432 -U oncoord -d property-scout -f schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS property_values (
    property_id              TEXT PRIMARY KEY,
    state                    TEXT,
    county                   TEXT,
    municipality             TEXT,
    parcel_id                TEXT,
    address                  TEXT,
    city                     TEXT,
    zip                      TEXT,
    latitude                 DOUBLE PRECISION,
    longitude                DOUBLE PRECISION,
    acreage                  DOUBLE PRECISION,
    assessed_value           DOUBLE PRECISION,
    assessed_land_value      DOUBLE PRECISION,
    assessed_building_value  DOUBLE PRECISION,
    assessment_year          INTEGER,
    last_sale_price          DOUBLE PRECISION,
    last_sale_date           DATE,
    building_sqft            DOUBLE PRECISION,
    bedrooms                 DOUBLE PRECISION,
    bathrooms                DOUBLE PRECISION,
    year_built               INTEGER,
    property_type            TEXT,
    source                   TEXT,
    source_url               TEXT,
    source_date              DATE,
    geometry                 GEOMETRY(Geometry, 4326),
    loaded_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS property_values_state_muni_idx ON property_values (state, municipality);
CREATE INDEX IF NOT EXISTS property_values_geometry_gix ON property_values USING GIST (geometry);
CREATE INDEX IF NOT EXISTS property_values_geography_gix ON property_values USING GIST ((geometry::geography));
CREATE INDEX IF NOT EXISTS property_values_property_type_idx ON property_values (property_type);
--
CREATE TABLE IF NOT EXISTS listings (
    listing_id          TEXT PRIMARY KEY,        -- RentCast's own id, e.g. "13-Maple-St,-Lincoln,-NH-03251"
    formatted_address    TEXT,
    address_line_1       TEXT,
    address_line_2       TEXT,
    city                 TEXT,
    state                TEXT,
    zip_code             TEXT,
    county               TEXT,
    latitude             DOUBLE PRECISION,
    longitude            DOUBLE PRECISION,
    geometry             GEOMETRY(Point, 4326),   -- generated from lat/lon, see trigger below

    property_type        TEXT,
    bedrooms              DOUBLE PRECISION,
    bathrooms             DOUBLE PRECISION,
    square_footage        DOUBLE PRECISION,
    lot_size              DOUBLE PRECISION,        -- RentCast: sq ft, confirm before mixing with `properties.acreage`
    year_built            INTEGER,

    status                TEXT,                    -- Active, Sold, etc.
    price                 DOUBLE PRECISION,
    listing_type          TEXT,
    listed_date           DATE,
    removed_date          DATE,
    days_on_market        INTEGER,

    mls_name              TEXT,
    mls_number            TEXT,

    agent                 JSONB,                    -- name/phone/email/website -- display-only, not queried
    office                JSONB,                    -- same
    price_history         JSONB,                    -- RentCast's `history` object, kept as-is -- a real
                                                    -- time series, not something to flatten into columns

    source                TEXT DEFAULT 'RentCast',
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS listings_state_city_idx ON listings (state, city);
CREATE INDEX IF NOT EXISTS listings_geometry_gix ON listings USING GIST (geometry);
CREATE INDEX IF NOT EXISTS listings_geography_gix ON listings USING GIST ((geometry::geography));
CREATE INDEX IF NOT EXISTS listings_status_idx ON listings (status);


-- Tracks WHEN an area was last fetched from RentCast, so the lazy-cache
-- layer can decide "do we already have recent enough data" without
-- scanning the listings table itself. scope_type/scope_value examples:
-- ('city', 'Lincoln,NH'), ('zip', '03251').
CREATE TABLE IF NOT EXISTS listing_fetch_log (
    id           SERIAL PRIMARY KEY,
    scope_type   TEXT NOT NULL,
    scope_value  TEXT NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_count INTEGER
);

CREATE INDEX IF NOT EXISTS listing_fetch_log_scope_idx ON listing_fetch_log (scope_type, scope_value, fetched_at DESC);


CREATE TABLE IF NOT EXISTS listing_source_cache (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT        NOT NULL,
    endpoint        TEXT        NOT NULL,
    request_key     TEXT        NOT NULL,
    request_params  JSONB       NOT NULL,
    response_body   JSONB       NOT NULL,
    status_code     INTEGER     NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NULL,
    hit_count       INTEGER     NOT NULL DEFAULT 0,
    last_hit_at     TIMESTAMPTZ NULL,
    CONSTRAINT uq_listing_source_cache_key UNIQUE (source, endpoint, request_key)
);