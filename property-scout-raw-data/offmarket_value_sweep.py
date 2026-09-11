"""
Generic /search/offmarket sweep (RealtyAPI/Zillow) -- ONE file, three
ways to use it depending on how much coverage you actually need. No
state-specific code -- works on any state's parcels GeoJSON or town
boundary polygon. First built for NH (which lacks a single authoritative
statewide assessed-value source, unlike VT/NJ/MA), but reusable by any
future state in the same situation -- hence living at the project root,
not spiders/nh/, matching join_parcels_offmarket.py's same reasoning.
Replaces offmarket_sweep.py, adaptive_offmarket_sweep.py, and
tile_town_offmarket.py (delete those -- this supersedes all three).

MODE 1 -- single call, exactly what you'd get from curl directly:
    python offmarket_value_sweep.py --zip 03784
    python offmarket_value_sweep.py --point 44.050588 -71.646983 --radius 0.25

MODE 2 -- one area, swept completely (recursive splitting per RealtyAPI
support's confirmed recipe: pageResultCount < 1000 means complete;
== 1000 means split into 4 and recurse):
    python offmarket_value_sweep.py --point 44.046551 -71.658718 --seed-radius 2 --complete

MODE 3 -- a whole town, swept completely (builds a seed grid from a
parcels geojson's actual footprint, then sweeps each seed cell like mode 2):
    python offmarket_value_sweep.py --town-parcels lincoln_nh_raw_parcels.geojson --complete

MODE 3b -- a whole town, using a REAL boundary polygon (admin9 GeoPackage)
instead of deriving extent from parcel centroids -- no parcels needed
first, decoupled from the GRANIT fetch entirely:
    python offmarket_value_sweep.py --town-boundary D:\data\old_data_polys\nam_admin_area_9.gpkg Lincoln NH --complete

Every mode writes one output JSON ({"offMarketResults": [...]}) plus a
one-line status to stdout. Modes 2/3 also print whether coverage is
confirmed complete or not, with specifics if not -- no separate manifest
file, no separate script to go read it in.

KNOWN LIMITATION, CONFIRMED (2026-09, RealtyAPI support): a single
zipCode call returns a SAMPLE, not a complete set, for any area with
meaningful listing density -- support's own words. Mode 1's --zip is the
cheap/fast/incomplete option; use --complete (mode 2/3) wherever
completeness actually matters.

--town-boundary requires geopandas (pip install geopandas) -- a new
dependency for this project; everything else here only needed shapely,
since it worked with GeoJSON already loaded in memory. Reading a .gpkg
with an attribute filter pushed down to GDAL (rather than loading all
143,589 admin9 features into memory first) needs the fuller library.
"""

import sys
import os
import json
import math
import time
import argparse

import requests

API_URL = "https://zillow.realtyapi.io/search/offmarket"
MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 2.0
PAGE_CAP = 1000  # confirmed page cap -- pageResultCount hitting this means the area needs splitting
MILES_PER_DEGREE_LAT = 69.0


# ---------- single call, with retry (used by every mode) ----------

def call_offmarket(params: dict, api_key: str) -> dict:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(API_URL, headers={"x-realtyapi-key": api_key}, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            message = str(payload.get("message", ""))
            if "no results found" in message.lower():
                # Legitimate, valid outcome -- genuinely zero off-market homes in this
                # area (e.g. deep forest, no nearby parcels at all) -- NOT a failure,
                # don't retry, don't raise. Normalize to the same shape a real empty
                # page would have so callers don't need a special case for this.
                return {"message": message, "pageResultCount": 0, "offMarketResults": []}
            if not message.startswith("200"):
                raise ValueError(f"unexpected message: {message!r}")
            return payload
        except Exception as e:
            last_err = e
            print(f"  attempt {attempt}/{MAX_RETRIES} failed for {params}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_SECONDS * attempt)
    raise RuntimeError(f"{params}: all {MAX_RETRIES} attempts failed -- last error: {last_err}")


# ---------- mode 2/3: recursive splitting ----------

def _child_cells(lat, lon, radius):
    half = radius / 2
    lat_off = half / MILES_PER_DEGREE_LAT
    lon_off = half / (MILES_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return [
        (lat + lat_off, lon + lon_off, half), (lat + lat_off, lon - lon_off, half),
        (lat - lat_off, lon + lon_off, half), (lat - lat_off, lon - lon_off, half),
    ]


def sweep_complete(lat, lon, radius, min_radius, api_key, seen_zpids, out_records,
                    incomplete_areas, call_counter, max_calls, visited_cells, depth=0):
    # Rounded to 4 decimals (~11m at NH's latitude), not 5 (~1m) -- CONFIRMED
    # too tight in practice: two independently-computed child coordinates
    # from different parent cells can land at the same real boundary point
    # but differ in the 6th decimal from floating-point accumulation, which
    # a 5-decimal round doesn't reliably catch (seen directly: 44.0444,
    # -71.6715 swept twice as two "different" cells in a real run). 4
    # decimals accepts a small risk of merging two genuinely-close-but-
    # different points -- an acceptable tradeoff given circles already
    # overlap substantially by design.
    cell_key = (round(lat, 4), round(lon, 4), round(radius, 5))
    if cell_key in visited_cells:
        prefix = "  " * depth
        print(f"{prefix}[{radius:.3f}mi @ {lat:.4f},{lon:.4f}] already swept (shared boundary with "
              f"an earlier seed cell) -- skipped, no new call")
        return

    if call_counter[0] >= max_calls:
        incomplete_areas.append((lat, lon, radius, "ABORTED-BUDGET"))
        return

    call_counter[0] += 1
    visited_cells.add(cell_key)
    payload = call_offmarket({"latitude": lat, "longitude": lon, "radius": radius}, api_key)
    page_count = payload.get("pageResultCount", 0)

    for r in payload.get("offMarketResults", []):
        zpid = r.get("zpid")
        if zpid is not None and zpid not in seen_zpids:
            seen_zpids.add(zpid)
            out_records.append(r)

    prefix = "  " * depth
    if page_count >= PAGE_CAP and radius > min_radius:
        print(f"{prefix}[{radius:.3f}mi @ {lat:.4f},{lon:.4f}] full page ({page_count}) -- splitting "
              f"({call_counter[0]}/{max_calls} calls used)")
        for c_lat, c_lon, c_rad in _child_cells(lat, lon, radius):
            sweep_complete(c_lat, c_lon, c_rad, min_radius, api_key, seen_zpids, out_records,
                            incomplete_areas, call_counter, max_calls, visited_cells, depth + 1)
    elif page_count >= PAGE_CAP:
        print(f"{prefix}[{radius:.3f}mi @ {lat:.4f},{lon:.4f}] WARNING: still full page at min-radius floor")
        incomplete_areas.append((lat, lon, radius, "INCOMPLETE-AT-FLOOR"))
    else:
        print(f"{prefix}[{radius:.3f}mi @ {lat:.4f},{lon:.4f}] complete ({page_count} results, "
              f"{call_counter[0]}/{max_calls} calls used)")


# ---------- mode 3: parcel-extent seed grid ----------

def parcel_centroids(parcels_geojson_path):
    with open(parcels_geojson_path) as f:
        data = json.load(f)
    centroids = []
    for feature in data.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        coords = []

        def collect(c):
            if isinstance(c[0], (int, float)):
                coords.append(c)
            else:
                for sub in c:
                    collect(sub)
        try:
            collect(geometry["coordinates"])
        except (KeyError, IndexError, TypeError):
            continue
        if coords:
            centroids.append((sum(c[1] for c in coords) / len(coords), sum(c[0] for c in coords) / len(coords)))
    return centroids


def seed_grid(centroids, seed_radius):
    if not centroids:
        return []
    mean_lat = sum(lat for lat, _ in centroids) / len(centroids)
    lat_step = seed_radius / MILES_PER_DEGREE_LAT
    lon_step = seed_radius / (MILES_PER_DEGREE_LAT * math.cos(math.radians(mean_lat)))
    cells = {(round(lat / lat_step), round(lon / lon_step)) for lat, lon in centroids}
    return [(c_lat * lat_step, c_lon * lon_step) for c_lat, c_lon in cells]


# ---------- mode 3b: real town boundary (admin9 GeoPackage) ----------

def load_town_boundary(boundaries_geojson_path: str, town_name: str, state_code: str):
    """
    Loads a town's boundary polygon from a pre-filtered NH boundaries
    GeoJSON (extract this ONCE from the full admin9 GeoPackage via
    ogr2ogr or QGIS's Save Features As -- see module docstring -- rather
    than reading the full 743MB/143,589-feature world file on every run).
    Plain json + shapely, same pattern as parcel_centroids() below and
    everywhere else in this project -- no geopandas dependency.

    Returns a single shapely geometry in WGS84 (lat/lon) -- unioned if
    the town matched more than one feature (e.g. a real MultiPolygon
    town, or overlapping source records).
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    with open(boundaries_geojson_path) as f:
        data = json.load(f)

    matches = [
        shape(feat["geometry"])
        for feat in data.get("features", [])
        if feat.get("properties", {}).get("name") == town_name
        and feat.get("properties", {}).get("a1_admin_code") == state_code
    ]

    if not matches:
        raise ValueError(
            f"No boundary found for {town_name}, {state_code} in {boundaries_geojson_path} -- "
            f"check spelling/casing exactly against the 'name' property, or whether this file "
            f"was filtered to the right state."
        )
    if len(matches) > 1:
        print(f"  NOTE: {len(matches)} matching feature(s) for {town_name}, {state_code} -- "
              f"unioning into one boundary")

    return unary_union(matches)


def seed_grid_from_boundary(boundary_geom, seed_radius: float) -> list:
    """
    Tiles the boundary's bounding box into seed_radius-sized cells,
    keeping a cell if its square footprint INTERSECTS the real boundary
    (not just "center falls inside" -- that stricter test could skip a
    genuine edge cell whose circle would cover real in-boundary parcels,
    just because its exact center point happened to fall a few meters
    outside the line). A few extra near-empty edge cells get swept as a
    result -- harmless, they just resolve fast with a low/zero count --
    better than silently missing real coverage right at the boundary.
    """
    from shapely.geometry import box

    minx, miny, maxx, maxy = boundary_geom.bounds
    mean_lat = (miny + maxy) / 2
    lat_step = seed_radius / MILES_PER_DEGREE_LAT
    lon_step = seed_radius / (MILES_PER_DEGREE_LAT * math.cos(math.radians(mean_lat)))

    tiles = []
    lat = miny
    while lat <= maxy:
        lon = minx
        while lon <= maxx:
            cell = box(lon - lon_step / 2, lat - lat_step / 2, lon + lon_step / 2, lat + lat_step / 2)
            if cell.intersects(boundary_geom):
                tiles.append((lat, lon))
            lon += lon_step
        lat += lat_step

    return tiles


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", help="mode 1: single zipCode call, no splitting")
    src.add_argument("--point", nargs=2, type=float, metavar=("LAT", "LON"),
                      help="mode 1 (with --radius) or mode 2 (with --complete)")
    src.add_argument("--town-parcels", help="mode 3: sweep a whole town's parcel extent, --complete implied")
    src.add_argument("--town-boundary", nargs=3, metavar=("GPKG_PATH", "TOWN_NAME", "STATE_CODE"),
                      help="mode 3b: sweep a town using its REAL boundary polygon from an admin9 "
                           "GeoPackage, instead of deriving extent from parcel centroids -- no "
                           "parcels needed first. Requires geopandas.")

    parser.add_argument("--radius", type=float, default=0.25, help="mode 1 --point radius, miles (default 0.25)")
    parser.add_argument("--complete", action="store_true", help="use recursive splitting for guaranteed completeness")
    parser.add_argument("--seed-radius", type=float, default=2.0, help="starting radius for --complete (default 2)")
    parser.add_argument("--min-radius", type=float, default=0.25, help="splitting floor for --complete (default 0.25)")
    parser.add_argument("--max-calls", type=int, default=200, help="hard call budget for --complete (default 200)")
    parser.add_argument("--out", default=None, help="output path (default: auto-named)")
    parser.add_argument("--api-key", default=os.environ.get("REALTYAPI_KEY"))
    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: set REALTYAPI_KEY or pass --api-key.")
        sys.exit(1)

    # ---- Mode 1: single call ----
    if args.zip or (args.point and not args.complete):
        if args.zip:
            params = {"zipCode": args.zip}
            out_path = args.out or f"nh_offmarket_zip{args.zip}.json"
        else:
            lat, lon = args.point
            params = {"latitude": lat, "longitude": lon, "radius": args.radius}
            out_path = args.out or f"nh_offmarket_{lat:.5f}_{lon:.5f}.json"

        payload = call_offmarket(params, args.api_key)
        with open(out_path, "w") as f:
            json.dump(payload, f)
        n = len(payload.get("offMarketResults", []))
        page_count = payload.get("pageResultCount")
        capped = page_count is not None and page_count >= PAGE_CAP
        print(f"{n} result(s) -> {out_path}")
        if capped:
            print(f"WARNING: pageResultCount={page_count} -- the page was FULL. This area has "
                  f"MORE homes than fit on one page; Zillow only considered the first {page_count} "
                  f"before stopping, and {n} of those happened to be off-market. This number is "
                  f"CONFIRMED INCOMPLETE -- use --complete for real coverage of this area.")
        else:
            print(f"pageResultCount={page_count} -- page was NOT full, so this IS everything "
                  f"Zillow holds for this area. Complete, per support's own signal.")
        return

    # ---- Mode 2/3: complete sweep ----
    seen_zpids, out_records, incomplete_areas, call_counter = set(), [], [], [0]
    visited_cells = set()

    if args.town_parcels:
        centroids = parcel_centroids(args.town_parcels)
        if not centroids:
            print(f"ERROR: no usable parcel centroids in {args.town_parcels}")
            sys.exit(1)
        seeds = seed_grid(centroids, args.seed_radius)
        print(f"{len(centroids)} parcel centroid(s) -> {len(seeds)} seed cell(s) at {args.seed_radius}mi")
        out_path = args.out or "nh_offmarket_town_complete.json"
    elif args.town_boundary:
        gpkg_path, town_name, state_code = args.town_boundary
        boundary = load_town_boundary(gpkg_path, town_name, state_code)
        seeds = seed_grid_from_boundary(boundary, args.seed_radius)
        print(f"Boundary for {town_name}, {state_code} -> {len(seeds)} seed cell(s) at "
              f"{args.seed_radius}mi (no parcels needed)")
        out_path = args.out or f"nh_offmarket_{town_name.lower()}_boundary_complete.json"
    else:
        seeds = [tuple(args.point)]
        out_path = args.out or f"nh_offmarket_{args.point[0]:.5f}_{args.point[1]:.5f}_complete.json"

    for i, (lat, lon) in enumerate(seeds, 1):
        print(f"\n=== seed {i}/{len(seeds)} ===")
        if call_counter[0] >= args.max_calls:
            print("  SKIPPED -- budget exhausted")
            incomplete_areas.append((lat, lon, args.seed_radius, "ABORTED-BUDGET"))
            continue
        sweep_complete(lat, lon, args.seed_radius, args.min_radius, args.api_key,
                        seen_zpids, out_records, incomplete_areas, call_counter, args.max_calls,
                        visited_cells)

    with open(out_path, "w") as f:
        json.dump({"offMarketResults": out_records}, f)

    status = "COMPLETE" if not incomplete_areas else "INCOMPLETE"
    print(f"\n{'=' * 50}")
    print(f"STATUS: {status}")
    print(f"{call_counter[0]} call(s), {len(out_records)} unique record(s) -> {out_path}")
    if incomplete_areas:
        print(f"{len(incomplete_areas)} area(s) not confirmed complete:")
        for lat, lon, rad, reason in incomplete_areas:
            print(f"  {reason} @ ({lat:.5f}, {lon:.5f}), radius {rad}mi")
    print(f"{'=' * 50}")
    sys.exit(0 if status == "COMPLETE" else 1)


if __name__ == "__main__":
    main()