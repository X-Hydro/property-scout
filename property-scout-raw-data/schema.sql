-- Property Values Database schema
-- Run once: psql -d propertyvalues -f schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS properties (
    property_id             TEXT PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS properties_state_muni_idx ON properties (state, municipality);
CREATE INDEX IF NOT EXISTS properties_geometry_gix ON properties USING GIST (geometry);