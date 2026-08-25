"""
RentCast listings loader — Property Values Database

Loads RentCast listing data into the `listings` table, keyed on
listing_id (RentCast's own id, e.g. "13-Maple-St,-Lincoln,-NH-03251").

DESIGNED FOR REUSE, not just a one-off: upsert_listings() is a standalone
function taking a list of already-parsed RentCast listing dicts and a
live DB connection -- this batch/file-based CLI is one caller of it, but
the future live "lazy load on gap-analysis request" service should
import and call the SAME function after its own API fetch, rather than
duplicating the upsert SQL in two places.

INPUT SHAPE: handles either of two file shapes automatically, since it
wasn't yet confirmed which one your RentCast downloads are saved as:
  1. A GeoJSON FeatureCollection (matching the convention used everywhere
     else in this project) -- each feature's `properties` holds the
     RentCast fields, `geometry` is a Point (or is ignored/rebuilt from
     lat/lon if absent -- see _listing_dict_from_feature).
  2. A raw JSON array of RentCast's own listing objects, exactly as
     shown in RentCast's API response (flat, latitude/longitude as
     top-level keys, no geometry wrapper).
A single bare listing object (dict, not a list/FeatureCollection) is
also accepted, in case a single-listing file is ever loaded.

Requires: psycopg2 (pip install psycopg2-binary), and schema.sql (with
the listings table, see listings_schema.sql) already applied.

Usage:
python load_listings.py nh_data/nh_lincoln_sfh_land.json --dsn "postgresql://oncoord:<pw>@localhost:5432/property-scout"
python load_listings.py rentcast_data/ --dsn "postgresql://..."  # directory of files,
                                                                 # non-recursive, same
                                                                 # convention as
                                                                 # load_property_values.py
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import date
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

# lat/lon can be NULL for a listing RentCast couldn't geocode -- guard
# the point-geometry expression so those rows still load (with a NULL
# geometry) instead of failing the whole batch.
ROW_TEMPLATE = (
    "(%(listing_id)s, " + ", ".join(f"%({c})s" for c in COLUMNS) +
    ", CASE WHEN %(longitude)s IS NOT NULL AND %(latitude)s IS NOT NULL "
    "THEN ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326) END)"
)


def _iso_date_only(raw) -> str | None:
    """RentCast dates come as full ISO datetimes ('2025-10-01T00:00:00.000Z')
    -- the listings table's date columns just want the date portion."""
    if not raw:
        return None
    try:
        return str(raw).split("T")[0]
    except AttributeError:
        return None


def _listing_dict_from_raw(raw: dict) -> dict:
    """Maps one RentCast listing object (RentCast's own field names) into
    a row dict matching the listings table's column names."""
    return {
        "listing_id": raw.get("id"),
        "formatted_address": raw.get("formattedAddress"),
        "address_line_1": raw.get("addressLine1"),
        "address_line_2": raw.get("addressLine2"),
        "city": raw.get("city"),
        "state": raw.get("state"),
        "zip_code": raw.get("zipCode"),
        "county": raw.get("county"),
        "latitude": raw.get("latitude"),
        "longitude": raw.get("longitude"),
        "property_type": raw.get("propertyType"),
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "square_footage": raw.get("squareFootage"),
        "lot_size": raw.get("lotSize"),  # NOTE: confirm units before comparing to property_values.acreage -- see listings_schema.sql
        "year_built": raw.get("yearBuilt"),
        "status": raw.get("status"),
        "price": raw.get("price"),
        "listing_type": raw.get("listingType"),
        "listed_date": _iso_date_only(raw.get("listedDate")),
        "removed_date": _iso_date_only(raw.get("removedDate")),
        "days_on_market": raw.get("daysOnMarket"),
        "mls_name": raw.get("mlsName"),
        "mls_number": raw.get("mlsNumber"),
        "agent": json.dumps(raw["listingAgent"]) if raw.get("listingAgent") is not None else None,
        "office": json.dumps(raw["listingOffice"]) if raw.get("listingOffice") is not None else None,
        "price_history": json.dumps(raw["history"]) if raw.get("history") is not None else None,
        "source": "RentCast",
    }
    # NOTE: RentCast also provides createdDate and lastSeenDate -- neither
    # has a column in the current listings schema, so both are dropped
    # here rather than silently guessed into an unrelated column. Add
    # columns for them first if you want to keep them.


def _listing_dict_from_feature(feature: dict) -> dict:
    """GeoJSON Feature shape -- properties already RentCast-named (same
    field names as the raw shape), geometry may or may not be present."""
    raw = dict(feature.get("properties", {}))
    row = _listing_dict_from_raw(raw)
    geom = feature.get("geometry")
    if geom and geom.get("type") == "Point" and row["longitude"] is None:
        # Fall back to the Feature's own geometry if the properties didn't
        # carry lat/lon directly (defensive -- expected shape has both).
        coords = geom.get("coordinates") or [None, None]
        row["longitude"], row["latitude"] = coords[0], coords[1]
    return row


def parse_listings_file(path: str) -> list[dict]:
    """Returns a list of row-dicts (listings table shape) from a file,
    auto-detecting FeatureCollection / raw array / single object."""
    with open(path) as f:
        data = json.load(f)

    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        return [_listing_dict_from_feature(feat) for feat in data.get("features", [])]
    if isinstance(data, list):
        return [_listing_dict_from_raw(raw) for raw in data]
    if isinstance(data, dict):
        return [_listing_dict_from_raw(data)]
    raise ValueError(f"{path}: unrecognized JSON shape (expected FeatureCollection, array, or object)")


def upsert_listings(conn, listings: list[dict]) -> int:
    """
    Reusable write path -- call this from the future live lazy-load
    service too (after its own RentCast API fetch), not just from this
    CLI, so the upsert logic only exists in one place.
    """
    if not listings:
        return 0

    no_id = sum(1 for r in listings if not r.get("listing_id"))
    if no_id:
        print(f"  WARNING: {no_id}/{len(listings)} listings have no listing_id -- "
              f"skipped (listing_id is the primary key)")
        listings = [r for r in listings if r.get("listing_id")]

    # Same defensive last-value-wins de-dup as load_property_values.py --
    # a duplicate key within one batch would otherwise crash the whole
    # upsert (Postgres can't ON CONFLICT DO UPDATE the same row twice in
    # one statement).
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
    """Same convention as load_property_values.py: directories expand to
    their files directly inside them, never recursing into subdirectories
    (so a folder like rentcast_data/bad/ stays excluded)."""
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


def main():
    if len(sys.argv) == 1:
        print("Usage:")
        print("  python load_listings.py <file-or-directory> --dsn \"<postgresql-dsn>\"")
        print()
        print("Example:")
        print('  python load_listings.py nh_data/nh_lincoln_sfh_land.json --dsn "postgresql://oncoord:<pw>@localhost:5432/property-scout"')
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "files",
        nargs="+",
        help="RentCast JSON/GeoJSON file(s) and/or directory/directories (non-recursive)"
    )
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()

    paths = _expand_paths(args.files)
    if not paths:
        print("ERROR: no .json/.geojson files found across the given path(s).")
        sys.exit(1)

    print(f"Loading {len(paths)} file(s)...")

    conn = psycopg2.connect(args.dsn)
    try:
        _check_schema_exists(conn)
        total = 0
        for path in paths:
            listings = parse_listings_file(path)
            n = upsert_listings(conn, listings)
            print(f"  {path}: upserted {n} listings")
            total += n
        print(f"\nDone. {total} total listings upserted across {len(paths)} file(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()