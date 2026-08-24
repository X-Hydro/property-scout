"""
PostGIS loader — Property Values Database

Reads GeoJSON files written by any spider (all normalized to the same
COMMON_SCHEMA_FIELDS, so this loader doesn't care which state/source a
file came from) and upserts into the `property_values` table, keyed on
`property_id` (e.g. "CT:43-86_0146978", "NH:121-077000-00") so re-running
a spider and reloading updates existing rows instead of duplicating them.

Requires: psycopg2 (pip install psycopg2-binary), shapely>=2.0 (pip install shapely)
for geometry repair, and schema.sql already applied (this script does NOT
create the table/extension itself -- schema changes are a separate,
explicit step):
    psql -d propertyvalues -f schema.sql

Usage:
    python load_property_values.py data/ct_bristol.geojson data/nh_lincoln.geojson \\
        --dsn "postgresql://user:pass@localhost:5432/propertyvalues"

# Any path that's a directory is expanded to every *.geojson file
# directly inside it (never recurses into subdirectories -- a folder
# like ma_data/bad/ holding known-bad output stays excluded even
# though it sits inside ma_data/):
python load_property_values.py ./nh_data/nh_lincoln.geojson  --dsn "postgresql://oncoord:oncoord-pg1@localhost:5432/property-scout"
python load_property_values.py ./ma_data/ma_andover.geojson  --dsn "postgresql://oncoord:oncoord-pg1@localhost:5432/property-scout"
python load_property_values.py ma_data/ --dsn "postgresql://oncoord:oncoord-pg1@localhost:5432/property-scout"
python load_property_values.py ma_data/ ct_data/ --dsn "postgresql://oncoord:oncoord-pg1@localhost:5432/property-scout"
python load_property_values.py ma_data/ nh_data/ma_andover.geojson --dsn "postgresql://oncoord:oncoord-pg1@localhost:5432/property-scout"

"""

import sys
import json
import argparse
from pathlib import Path
import psycopg2
import psycopg2.extras
from shapely.geometry import shape, mapping, MultiPoint
from shapely.validation import make_valid

# Every non-PK column, in the same order as the INSERT statement below.
COLUMNS = [
    "state", "county", "municipality", "parcel_id", "address", "city", "zip",
    "latitude", "longitude", "acreage", "assessed_value", "assessed_land_value",
    "assessed_building_value", "assessment_year", "last_sale_price", "last_sale_date",
    "building_sqft", "bedrooms", "bathrooms", "year_built", "property_type",
    "source", "source_url", "source_date",
]

UPSERT_SQL = f"""
INSERT INTO property_values (property_id, {", ".join(COLUMNS)}, geometry)
VALUES %s
ON CONFLICT (property_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)},
    geometry = EXCLUDED.geometry,
    loaded_at = now()
"""

# Template for each row's VALUES tuple -- geometry needs ST_SetSRID(ST_GeomFromGeoJSON(...), 4326),
# so it can't be a plain positional %s like the other columns; psycopg2.extras.execute_values
# handles this via a per-row template string.
ROW_TEMPLATE = (
    "(%(property_id)s, " + ", ".join(f"%({c})s" for c in COLUMNS) +
    ", ST_SetSRID(ST_GeomFromGeoJSON(%(geometry_json)s), 4326))"
)


def _extract_points(geometry: dict) -> list[tuple[float, float]]:
    """
    Recursively walks a GeoJSON geometry's `coordinates` array and
    collects every (lon, lat) pair found, at any nesting depth (Polygon
    exterior/interior rings, MultiPolygon parts, etc.), regardless of
    whether the geometry as a whole is topologically valid. Used as the
    last-resort salvage path in _repair_geometry() below -- the raw
    vertices are still real coordinates even when their connectivity/
    winding isn't.
    """
    coords = (geometry or {}).get("coordinates")
    if not coords:
        return []
    points: list[tuple[float, float]] = []

    def _walk(c):
        # A coordinate pair is exactly [number, number, ...]; anything
        # else is a nested list to recurse into.
        if (isinstance(c, (list, tuple)) and len(c) >= 2
                and all(isinstance(v, (int, float)) for v in c[:2])):
            points.append((c[0], c[1]))
        elif isinstance(c, (list, tuple)):
            for sub in c:
                _walk(sub)

    _walk(coords)
    return points


def _repair_geometry(geometry: dict | None, property_id: str) -> tuple[dict | None, str]:
    """
    Attempts to repair an invalid polygon before load, using the same
    tool (ST_MakeValid's Shapely equivalent) that cleanly fixed all
    8,661 pre-existing bad rows across CT/MA/NH in a one-off Postgres
    UPDATE on 2026-08-24 -- e.g. MassGIS's Esri-sourced condo/PUD
    parcels whose second ring is a genuinely separate polygon part,
    mislabeled as a "hole" that lies outside the first ring's shell.
    Doing it here means every future load repairs on the way in,
    instead of needing another one-off cleanup pass later.

    If make_valid() itself can't produce a usable result, tries buffer(0)
    as a last-resort second attempt (see inline comment below for why
    it's ordered after, not before, make_valid()) before giving up on
    repair entirely and falling back to a Point at the centroid of the
    convex hull of every raw vertex found in the original geometry (see
    _extract_points). That Point can never resolve as a TARGET parcel
    (point-in-polygon ST_Contains requires an actual polygon -- a point
    can't contain another point), but it's a valid input to nearby-comp
    distance search (ST_DWithin works fine between two points), so the
    property's assessed value still contributes to comp medians instead
    of vanishing entirely.

    Returns (geojson_dict_or_None, status):
      "ok"             -- no geometry supplied, or it was already valid;
                          dict is the original geometry unchanged
      "repaired"       -- was invalid, make_valid() fixed it into a
                          usable Polygon/MultiPolygon; dict is that
                          repaired geometry
      "point_fallback" -- couldn't be repaired into a usable polygon,
                          but real vertex coordinates existed; dict is
                          a Point at their convex hull's centroid
      "discarded"      -- no usable coordinates could be salvaged at
                          all; dict is None, caller loads geometry=NULL
    """
    if not geometry:
        return None, "ok"

    try:
        geom = shape(geometry)
        if geom.is_valid and not geom.is_empty:
            return geometry, "ok"

        repaired = make_valid(geom)
        if not repaired.is_empty and repaired.geom_type in ("Polygon", "MultiPolygon"):
            return mapping(repaired), "repaired"

        # make_valid() itself didn't produce anything usable -- not seen
        # once across the 8,661 known-bad CT/MA/NH rows (make_valid()
        # cleared all of them), so this is a low-cost extra attempt for
        # whatever future/different source might hit a case it can't
        # resolve. buffer(0) is an older, less standards-based repair
        # heuristic that make_valid() was specifically added to GEOS/
        # Shapely to supersede -- tried only here, as a last resort
        # after make_valid() has already failed, never in place of it.
        buffered = geom.buffer(0)
        if not buffered.is_empty and buffered.geom_type in ("Polygon", "MultiPolygon"):
            print(f"    NOTE: {property_id}: make_valid() failed, buffer(0) repaired it instead")
            return mapping(buffered), "repaired"
    except Exception as e:
        print(f"    WARNING: {property_id}: geometry parse/repair failed ({e}) -- attempting point fallback")

    points = _extract_points(geometry)
    if not points:
        print(f"    WARNING: {property_id}: no coordinates could be salvaged -- loading with geometry=NULL")
        return None, "discarded"

    try:
        hull_centroid = MultiPoint(points).convex_hull.centroid
        print(f"    WARNING: {property_id}: geometry unrepairable -- loaded as centroid "
              f"Point of {len(points)} vertices")
        return mapping(hull_centroid), "point_fallback"
    except Exception as e:
        print(f"    WARNING: {property_id}: convex hull centroid failed ({e}) -- loading with geometry=NULL")
        return None, "discarded"


def _row_from_feature(feature: dict) -> dict:
    props = dict(feature.get("properties", {}))
    geometry = feature.get("geometry")
    property_id = props.get("property_id") or "<unknown>"
    repaired_geometry, geom_status = _repair_geometry(geometry, property_id)
    props["geometry_json"] = json.dumps(repaired_geometry) if repaired_geometry else None
    # Not a real column -- read and stripped by load_file() to build the
    # per-file repaired/discarded summary, never referenced by
    # ROW_TEMPLATE, so it's harmless to leave on the dict until then.
    props["_geom_status"] = geom_status
    # Every COMMON_SCHEMA_FIELDS key should already be present (spiders
    # validate this), but default missing keys to None defensively rather
    # than KeyError on a file from an older/different spider version.
    for c in COLUMNS + ["property_id"]:
        props.setdefault(c, None)
    return props


def load_file(conn, path: str) -> int:
    with open(path) as f:
        fc = json.load(f)
    features = fc.get("features", [])
    if not features:
        print(f"  {path}: 0 features, skipping")
        return 0

    rows = [_row_from_feature(feat) for feat in features]

    geom_statuses = [r.pop("_geom_status", "ok") for r in rows]
    n_repaired = geom_statuses.count("repaired")
    n_point_fallback = geom_statuses.count("point_fallback")
    n_discarded = geom_statuses.count("discarded")
    if n_repaired or n_point_fallback or n_discarded:
        print(f"  {path}: geometry -- {n_repaired} repaired, {n_point_fallback} "
              f"point-fallback, {n_discarded} discarded (geometry=NULL)")

    no_property_id = sum(1 for r in rows if not r.get("property_id"))
    if no_property_id:
        print(f"  WARNING: {no_property_id}/{len(rows)} rows in {path} have no "
              f"property_id -- these will fail the upsert (property_id is the "
              f"primary key) and are skipped")
        rows = [r for r in rows if r.get("property_id")]

    # Defensive de-dup: Postgres's ON CONFLICT DO UPDATE cannot affect the
    # same row twice within one statement, so a duplicate property_id
    # would crash the whole batch, not just that row. The spider is
    # responsible for real de-duplication (see ct_spider.py's
    # _dedupe_property_ids, which disambiguates with a real internal ID
    # rather than silently dropping records) -- this is only a last-resort
    # safety net for a file generated before that fix, or from a spider
    # that doesn't dedupe yet. Last-value-wins here, so a genuinely
    # up-to-date spider file should never actually hit this branch.
    seen = {}
    for r in rows:
        seen[r["property_id"]] = r
    if len(seen) < len(rows):
        print(f"  WARNING: {len(rows) - len(seen)} duplicate property_id row(s) in "
              f"{path} collapsed via last-value-wins -- this should be fixed at the "
              f"spider level (see ct_spider.py's de-dupe), not relied on here")
    rows = list(seen.values())

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, UPSERT_SQL, rows, template=ROW_TEMPLATE, page_size=500)
    conn.commit()
    print(f"  {path}: upserted {len(rows)} rows")
    return len(rows)


def _expand_paths(paths: list[str]) -> list[str]:
    """
    Each entry in `paths` can be a .geojson file OR a directory -- a
    directory is expanded to every *.geojson file directly inside it.
    NEVER recurses into subdirectories -- deliberate, since a directory
    like ma_data/bad/ (holding known-bad output to keep out of a normal
    load) should never get silently swept in just because it happens to
    sit inside the folder being loaded.
    Non-existent paths are reported and skipped rather than silently
    dropped or raising, since a typo'd path shouldn't kill the whole run
    after other real paths were already validated.
    """
    expanded = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found = sorted(path.glob("*.geojson"))
            if not found:
                print(f"  WARNING: {p} is a directory with no *.geojson files "
                      f"directly inside it -- nothing to load from it "
                      f"(subdirectories are never searched)")
            expanded.extend(str(f) for f in found)
        elif path.is_file():
            expanded.append(str(path))
        else:
            print(f"  WARNING: {p} does not exist -- skipped")
    return expanded


def _check_schema_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.property_values')")
        exists = cur.fetchone()[0] is not None
    if not exists:
        print("ERROR: 'property_values' table doesn't exist. Run schema.sql first:\n"
              "  psql -d <dbname> -f schema.sql")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="GeoJSON file(s) and/or directory/directories "
                                                    "(directories are expanded to their *.geojson files, "
                                                    "never recursing into subdirectories)")
    parser.add_argument("--dsn", required=True, help="Postgres connection string, "
                                                        "e.g. postgresql://user:pass@host:5432/dbname")
    args = parser.parse_args()

    paths = _expand_paths(args.files)
    if not paths:
        print("ERROR: no .geojson files found across the given path(s).")
        sys.exit(1)
    print(f"Loading {len(paths)} file(s)...")

    conn = psycopg2.connect(args.dsn)
    try:
        _check_schema_exists(conn)

        total = 0
        for path in paths:
            total += load_file(conn, path)
        print(f"\nDone. {total} total rows upserted across {len(paths)} file(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()