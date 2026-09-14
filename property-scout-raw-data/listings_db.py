"""
Shared `listings` table write path — Property Values Database

Extracted out from load_listings_rentcast.py so that no single provider's
loader doubles as "the shared library" for the others -- every provider
loader (RentCast, RealtyAPI/Realtor, and any future one) imports from
THIS file as an equal, none of them owns it. Same convention as
spiders/common/ on the property_values side of this project: one shared
file for the genuinely source-agnostic plumbing, one file per source for
its own parsing/mapping.

Holds: column list, upsert SQL, the batch-write function, and the couple
of small generic helpers (date normalization, file/directory expansion)
that turned out to be provider-agnostic too. NO provider-specific field
mapping lives here -- that stays in each provider's own loader.

Requires: psycopg2 (pip install psycopg2-binary)
"""

import sys
from pathlib import Path
import psycopg2
import psycopg2.extras

COLUMNS = [
    "formatted_address", "address_line_1", "address_line_2", "city", "state",
    "zip_code", "county", "latitude", "longitude", "property_type", "bedrooms",
    "bathrooms", "square_footage", "lot_size", "year_built", "status", "price",
    "listing_type", "listed_date", "removed_date", "days_on_market",
    "mls_name", "mls_number", "agent", "office", "price_history", "source",
]

UPSERT_SQL = f"""
INSERT INTO listings (listing_id, {", ".join(COLUMNS)}, geometry)
VALUES %s
ON CONFLICT (listing_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)},
    geometry = EXCLUDED.geometry,
    fetched_at = now()
"""

# lat/lon can be NULL for a listing a provider couldn't geocode -- guard
# the point-geometry expression so those rows still load (with a NULL
# geometry) instead of failing the whole batch.
ROW_TEMPLATE = (
    "(%(listing_id)s, " + ", ".join(f"%({c})s" for c in COLUMNS) +
    ", CASE WHEN %(longitude)s IS NOT NULL AND %(latitude)s IS NOT NULL "
    "THEN ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326) END)"
)


def _iso_date_only(raw) -> str | None:
    """Providers vary on date format (RentCast: full ISO datetime like
    '2025-10-01T00:00:00.000Z') -- the listings table's date columns just
    want the date portion either way."""
    if not raw:
        return None
    try:
        return str(raw).split("T")[0]
    except AttributeError:
        return None


def upsert_listings(conn, listings: list[dict]) -> int:
    """
    THE single write path for the `listings` table. Every provider loader
    calls this after its own parsing/mapping -- none of them should ever
    contain their own INSERT/UPDATE SQL. Takes a list of already-mapped
    row dicts (keys matching COLUMNS + listing_id) and a live DB connection.
    """
    if not listings:
        return 0

    no_id = sum(1 for r in listings if not r.get("listing_id"))
    if no_id:
        print(f"  WARNING: {no_id}/{len(listings)} listings have no listing_id -- "
              f"skipped (listing_id is the primary key)")
        listings = [r for r in listings if r.get("listing_id")]

    # Defensive last-value-wins de-dup -- a duplicate key within one batch
    # would otherwise crash the whole upsert (Postgres can't ON CONFLICT DO
    # UPDATE the same row twice in one statement).
    seen = {}
    for r in listings:
        seen[r["listing_id"]] = r
    if len(seen) < len(listings):
        print(f"  WARNING: {len(listings) - len(seen)} duplicate listing_id row(s) "
              f"collapsed via last-value-wins")
    rows = list(seen.values())

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, UPSERT_SQL, rows, template=ROW_TEMPLATE, page_size=500)
    conn.commit()
    return len(rows)


def _expand_paths(paths: list[str]) -> list[str]:
    """Directories expand to their files directly inside them, never
    recursing into subdirectories (so a folder like rentcast_data/bad/
    stays excluded). Same convention across all provider loaders."""
    expanded = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found = sorted(path.glob("*.json")) + sorted(path.glob("*.geojson"))
            if not found:
                print(f"  WARNING: {p} is a directory with no *.json/*.geojson files "
                      f"directly inside it")
            expanded.extend(str(f) for f in found)
        elif path.is_file():
            expanded.append(str(path))
        else:
            print(f"  WARNING: {p} does not exist -- skipped")
    return expanded


def _check_schema_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.listings')")
        exists = cur.fetchone()[0] is not None
    if not exists:
        print("ERROR: 'listings' table doesn't exist. Run listings_schema.sql first:\n"
              "  psql -d <dbname> -f listings_schema.sql")
        sys.exit(1)