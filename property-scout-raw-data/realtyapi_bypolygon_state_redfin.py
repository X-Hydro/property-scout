"""
RealtyAPI "bypolygon" full-state search -- REDFIN VARIANT.

Same Census-boundary-driven polygon search as realtyapi_bypolygon_state.py
(Realtor.com version), retargeted at Redfin's /search/bypolygon endpoint.
Read that script's docstring first for the shared geometry/pagination
design (multi-part states, cost-bounded top-N-by-area part selection,
dedupe strategy, URL length risk, etc.) -- this file only documents
what's DIFFERENT for Redfin, confirmed by direct testing on 2026-09-06.

CONFIRMED DIFFERENCES FROM THE REALTOR.COM VERSION:

1. BASE URL: https://redfin.realtyapi.io/search/bypolygon (not
   realtor.realtyapi.io).

2. FORECLOSURE FILTER PARAM NAME AND VALUE ARE DIFFERENT, AND ONE OF THE
   OBVIOUS-LOOKING OPTIONS IS FLAT-OUT WRONG:
     - status=Foreclosure -- WRONG. Silently rejected; response message
       says "searchType not applied, added For_Sale by default" and
       returns a normal unfiltered search. Confirmed on /search/bylocation.
     - listingType=MLS listed Foreclosures -- CORRECT. Confirmed working
       on BOTH /search/bylocation (North Andover, MA: 41 unfiltered ->
       1 filtered) AND /search/bypolygon (Detroit-area box: returned 3
       real foreclosure listings, 2 of which cross-validated against
       independently-confirmed Realtor.com foreclosure listings at the
       identical street addresses -- 8265 Cloverlawn St and 9186 Steel
       St, Detroit MI). The exact string, including capitalization and
       spaces, matters -- "MLS Listed Foreclosures" or "foreclosures"
       have NOT been tested and should not be assumed to work.
     - The API's response message ("searchType not applied...") is
       UNRELIABLE as a success/failure signal: it printed the exact same
       text on the confirmed-WORKING listingType=MLS listed Foreclosures
       call as it did on the confirmed-BROKEN status=Foreclosure call.
       Do not use data["message"] to decide whether a filter worked --
       only the actual result count/content tells you that.

3. RESPONSE SCHEMA IS NESTED DIFFERENTLY. Realtor.com returns flat
   objects. Redfin wraps each result as {"homeData": {...},
   "defaultExtension": {}}. Field path differences that matter here:
     - Street address:  homeData.addressInfo.formattedStreetLine
       (Realtor.com: address.line)
     - City:            homeData.addressInfo.city
       (Realtor.com: address.city)
     - Unique ID:        homeData.propertyId
       (this DOES match one of CANDIDATE_ID_FIELDS from the Realtor.com
       script unchanged, since dedupe reads whatever field is actually
       present in the result dict -- see NESTED DEDUPE note below)
     - Price:           homeData.priceInfo.amount
       (Realtor.com: list_price, a bare number; Redfin's is a string)

4. NESTED DEDUPE: the Realtor.com script's _pick_id_field() /
   _dedupe_key() functions scan the TOP LEVEL of each result dict for a
   candidate ID field. Redfin's real ID (propertyId) lives inside
   homeData, not at the top level -- so those functions are reused here
   but are called against result["homeData"], not result itself. This is
   the one real code change beyond swapping URLs/params, not just a
   config difference.

5. NO CONFIRMED "total" FIELD. Every Redfin response seen in this
   project (bylocation and bypolygon alike) has resultCount and
   nextPage, but no total. This matters because the Realtor.com script's
   CONFIRMED pagination-cap safety check (comparing fetched count against
   the last-reported total before a suspicious empty page) CANNOT be
   replicated here -- there is nothing to compare against. This script
   trusts nextPage alone to end pagination. IF Redfin has a similar
   silent result-window cap to the one confirmed on Realtor.com (~10,000
   results / 50 pages), THIS SCRIPT WOULD NOT DETECT IT the way the
   Realtor.com version does. This is an unconfirmed, unmitigated risk --
   flagged loudly here rather than silently assumed away. If a
   Redfin-backed run for a high-volume state looks suspiciously round or
   suspiciously capped, this is the first thing to suspect.

6. PROPERTY TYPE FILTER NAME AND VOCABULARY ARE ALSO DIFFERENT AND
   UNCONFIRMED ON THIS ENDPOINT SPECIFICALLY. Redfin's docs (for
   /search/bylocation, /bycoordinates, /byregionid -- bypolygon's own doc
   page was not re-checked field-by-field) describe a `homeType` filter
   taking text values (House, Condo, Townhouse, Multi family, Land,
   Other, Mobile, Co-op) -- NOT `propertyType`, and a different
   vocabulary than Realtor.com's. This has NOT been tested on
   /search/bypolygon in this project (only listingType was verified
   there). Default behavior here matches the Realtor.com script's latest
   revision: omit the filter entirely unless the caller passes
   --home-type, on the same "optional filter, omission should mean all
   types" assumption -- which is exactly as unverified here as it was
   there. Confirm empirically (compare a dry-run total with and without
   an explicit --home-type list) before trusting it for a real state run,
   same as recommended for the Realtor.com script's --property-type.

Everything else (Census geometry loading, top-N-by-area part selection
for multi-part states, ring-to-polygon-string conversion, retry/backoff,
CLI shape, dry-run mode) is unchanged from the Realtor.com script and not
re-documented here -- read that file's docstring for the full design
rationale.

Usage:
    pip install pyproj
    set REALTYAPI_KEY=...          (Windows)
    export REALTYAPI_KEY=...       (bash)
    python realtyapi_bypolygon_state_redfin.py MI --dry-run
    python realtyapi_bypolygon_state_redfin.py MA --out ma_foreclosures_redfin.json
"""

import argparse
import hashlib
import json
import os
import sys
import time

import requests
from pyproj import Geod
from shapely.geometry import Polygon

API_URL = "https://redfin.realtyapi.io/search/bypolygon"
GEOJSONL_PATH_DEFAULT = "d:/data/us_state_boundaries/cp_2025_us_state_20m.geojsonl"
PAGE_SLEEP_SECONDS = 0.5
MAX_RETRIES = 3
MAX_PARTS_DEFAULT = 5
MIN_PART_AREA_KM2_DEFAULT = 10.0  # see Realtor.com script docstring -- currently a no-op

# CONFIRMED working value, exact string -- see docstring item 2. This
# script is foreclosure-only by design (unlike the Realtor.com version's
# optional --foreclosure flag), since listingType is Redfin's *only*
# confirmed working distress filter and there is no separate
# bank-owned/REO-specific value confirmed for this endpoint.
LISTING_TYPE_FORECLOSURE = "MLS listed Foreclosures"

RESULT_COUNT = 200

# CONFIRMED (2026-09-06): Redfin's /search/bypolygon rejects a real,
# valid, correctly-closed 215-point Michigan state polygon outright --
# every request returns {"message":"404: Error", "searchResults":[]},
# regardless of other params (page, listingType, or their absence). The
# IDENTICAL polygon string works fine on Realtor.com's /search/bypolygon
# (returned 251 real listings there). A simplified 5-point bounding box
# covering the SAME real geographic extent succeeded immediately on
# Redfin (confirmed: real listings returned, nextPage:true). This
# confirms the failure is about polygon complexity/vertex count, not
# geography, URL length in the abstract, or a params issue.
#
# SIMPLIFICATION STRATEGY: rather than guess at a vertex-count ceiling
# between the confirmed-failing 215 and the confirmed-working 5, this
# script trades shape precision for a method KNOWN to work: reduce each
# selected part to either its bounding box (5 points, confirmed working)
# or its convex hull (fewer points than the original but not yet tested
# against this endpoint, and geometrically LARGER than the true state
# shape wherever the coastline/border is concave). Either way this means
# searching a larger area than the real state -- see the overshoot % this
# script prints per part, and cross-reference against the Realtor.com
# script's REJECTED ALTERNATIVE note (convex hull risked 13.6x overshoot
# for Alaska specifically) before assuming a given state's overshoot is
# harmless.
SIMPLIFY_METHOD_DEFAULT = "bbox"

_GEOD = Geod(ellps="WGS84")

CANDIDATE_ID_FIELDS = ["propertyId", "id", "property_id", "listingId", "listing_id", "mlsId", "mls_id"]


class ScriptError(Exception):
    pass


def _ring_area_km2(ring: list[list[float]]) -> float:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    area, _ = _GEOD.polygon_area_perimeter(lons, lats)
    return abs(area) / 1e6


def load_state_geometry(geojsonl_path: str, state_code: str) -> list[list[list[float]]]:
    """Unchanged from the Realtor.com script -- see that file for full
    documentation of this function's behavior and limitations."""
    state_code = state_code.strip().upper()
    with open(geojsonl_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            feature = json.loads(line)
            props = feature.get("properties", {})
            if props.get("STUSPS") != state_code:
                continue

            geometry = feature.get("geometry", {})
            gtype = geometry.get("type")
            coords = geometry.get("coordinates")

            if gtype == "Polygon":
                parts = [coords]
            elif gtype == "MultiPolygon":
                parts = coords
            else:
                raise ScriptError(f"{geojsonl_path}:{line_no}: unexpected geometry type "
                                   f"'{gtype}' for {state_code} -- expected Polygon or MultiPolygon")

            rings = []
            for part_idx, part in enumerate(parts):
                if len(part) != 1:
                    raise ScriptError(
                        f"{geojsonl_path}:{line_no}: {state_code} part {part_idx} has "
                        f"{len(part)} rings (a hole/enclave) -- this script only handles "
                        f"single-ring parts"
                    )
                rings.append(part[0])
            return rings

    raise ScriptError(f"No feature with STUSPS == '{state_code}' found in {geojsonl_path}")


def select_parts(rings: list[list[list[float]]], max_parts: int,
                  min_area_km2: float) -> tuple[list[list[list[float]]], list[float], list[float]]:
    """Unchanged from the Realtor.com script."""
    with_area = sorted(((r, _ring_area_km2(r)) for r in rings), key=lambda x: -x[1])
    top = with_area[:max_parts]
    dropped_by_rank = [a for _, a in with_area[max_parts:]]

    kept = [(r, a) for r, a in top if a >= min_area_km2]
    dropped_by_size = [a for _, a in top if a < min_area_km2]

    return [r for r, _ in kept], dropped_by_rank, dropped_by_size


def bbox_ring(ring: list[list[float]]) -> list[list[float]]:
    """CONFIRMED working on Redfin's /search/bypolygon (2026-09-06,
    Michigan). 5-point rectangle -- max overshoot, min vertex count."""
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    return [
        [min_lon, min_lat], [max_lon, min_lat],
        [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat],
    ]


def convex_hull_ring(ring: list[list[float]]) -> list[list[float]]:
    """NOT YET TESTED against Redfin's /search/bypolygon -- vertex count
    is typically well under 215 for real state boundaries (a hull can
    only have as many vertices as the original ring, usually far fewer),
    but whether it's under whatever ceiling caused the 404 is unconfirmed.
    Geometrically tighter than bbox_ring() for any non-rectangular shape,
    but still overshoots the true state boundary wherever the coastline/
    border is concave (bays, panhandles, etc.) -- see the overshoot %
    this script prints before trusting a hull-based run's coverage."""
    hull = Polygon(ring).convex_hull
    coords = list(hull.exterior.coords)
    return [[lon, lat] for lon, lat in coords]


def simplify_ring(ring: list[list[float]], method: str) -> tuple[list[list[float]], float]:
    """Returns (simplified_ring, overshoot_ratio) where overshoot_ratio
    is simplified_area / true_area (1.0 = no overshoot, 2.0 = simplified
    shape covers 2x the true state area). Uses the same real geodesic
    area calculation (_ring_area_km2) the part-selection ranking already
    relies on, not raw degrees^2, for consistency with the rest of this
    script's area reasoning."""
    true_area = _ring_area_km2(ring)
    if method == "bbox":
        simplified = bbox_ring(ring)
    elif method == "hull":
        simplified = convex_hull_ring(ring)
    elif method == "none":
        return ring, 1.0
    else:
        raise ScriptError(f"unknown --simplify method '{method}' -- expected bbox, hull, or none")
    simplified_area = _ring_area_km2(simplified)
    overshoot = simplified_area / true_area if true_area > 0 else float("inf")
    return simplified, overshoot


def ring_to_polygon_param(ring: list[list[float]]) -> str:
    return ",".join(f"{lon} {lat}" for lon, lat in ring)


def _request_with_retries(headers: dict, params: dict) -> dict:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(API_URL, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            print(f"  attempt {attempt}/{MAX_RETRIES} failed ({e}); retrying in {2 * attempt}s")
            time.sleep(2 * attempt)
    raise ScriptError(f"request failed after {MAX_RETRIES} attempts: {last_exc}")


def _pick_id_field(sample_home_data: dict) -> str | None:
    """CHANGED from the Realtor.com script: takes the inner homeData dict,
    not the top-level result -- see docstring item 4."""
    for field in CANDIDATE_ID_FIELDS:
        if field in sample_home_data:
            return field
    return None


def _dedupe_key(result: dict, id_field: str | None) -> str:
    """CHANGED: reads id_field from result['homeData'], not result
    itself -- see docstring item 4."""
    home_data = result.get("homeData", {})
    if id_field is not None:
        return f"{id_field}:{home_data.get(id_field)}"
    return "hash:" + hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def _address_summary(result: dict) -> str:
    """CHANGED field paths vs Realtor.com -- see docstring item 3."""
    home_data = result.get("homeData", {})
    addr = home_data.get("addressInfo", {})
    line = addr.get("formattedStreetLine", "?")
    city = addr.get("city", "?")
    return f"{line}, {city}"


def dry_run_polygon(ring: list[list[float]], api_key: str, home_type: str | None,
                     foreclosure_only: bool = True) -> tuple[int, int]:
    """CHANGED from the Realtor.com version: no confirmed 'total' field
    on Redfin (see docstring item 5), so this can only report the page-1
    result count, NOT a true total or an accurate page estimate. Treat
    the returned 'pages' value as a lower bound, not a real estimate --
    if page 1 is full (RESULT_COUNT items) there may be more pages
    beyond what this can predict without walking them."""
    headers = {"x-realtyapi-key": api_key}
    params = {
        "polygon": ring_to_polygon_param(ring),
        "page": 1,
        "resultCount": RESULT_COUNT,
    }
    if foreclosure_only:
        params["listingType"] = LISTING_TYPE_FORECLOSURE
    if home_type:
        params["homeType"] = home_type
    data = _request_with_retries(headers, params)
    count = len(data.get("searchResults", []))
    # Lower-bound-only page estimate -- see function docstring.
    pages_lower_bound = 1 if count < RESULT_COUNT else 2  # "2+" signal, not a real count
    return count, pages_lower_bound


def search_polygon(ring: list[list[float]], api_key: str, home_type: str | None,
                    part_label: str, foreclosure_only: bool = True) -> list[dict]:
    headers = {"x-realtyapi-key": api_key}
    params = {
        "polygon": ring_to_polygon_param(ring),
        "page": 1,
        "resultCount": RESULT_COUNT,
    }
    if foreclosure_only:
        params["listingType"] = LISTING_TYPE_FORECLOSURE
    if home_type:
        params["homeType"] = home_type

    part_results = []
    while True:
        data = _request_with_retries(headers, params)
        results = data.get("searchResults", [])
        part_results.extend(results)
        print(f"  [{part_label}] page {params['page']}: {len(results)} listings "
              f"({len(part_results)} so far)")
        if not data.get("nextPage"):
            break
        params["page"] += 1
        time.sleep(PAGE_SLEEP_SECONDS)

    # UNCONFIRMED WHETHER THIS IS NEEDED: unlike the Realtor.com script,
    # there is no "total" field here to detect a silent pagination cap
    # (see docstring item 5). If this part's count comes back suspiciously
    # round (e.g. exactly a multiple of RESULT_COUNT with nextPage still
    # somehow false) that's worth manually double-checking against the
    # site/another tool -- this script cannot detect that condition on
    # its own the way the Realtor.com version can.
    if len(part_results) % RESULT_COUNT == 0 and len(part_results) > 0:
        print(f"  NOTE [{part_label}]: fetched count ({len(part_results)}) is an exact multiple "
              f"of RESULT_COUNT ({RESULT_COUNT}) with nextPage=false -- plausibly just a coincidence, "
              f"but this is exactly the shape a silent pagination cap would produce and this script "
              f"has no 'total' field to check it against. Worth a manual sanity check for "
              f"high-volume states.")

    return part_results


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""example:
  python realtyapi_bypolygon_state_redfin.py MI --dry-run
  python realtyapi_bypolygon_state_redfin.py MA --out ma_foreclosures_redfin.json
""",
    )
    parser.add_argument("state", help="2-letter state abbreviation, e.g. MI")
    parser.add_argument("--geojsonl", default=GEOJSONL_PATH_DEFAULT,
                         help=f"path to the .geojsonl file, one Feature per line "
                              f"(default: {GEOJSONL_PATH_DEFAULT})")
    parser.add_argument("--out", help="output JSON path (default: realtyapi_<state>_foreclosures_redfin.json, "
                                       "or realtyapi_<state>_all_redfin.json with --all-listings)")
    parser.add_argument("--all-listings", action="store_true",
                         help="skip the foreclosure filter entirely and pull every current listing "
                              "in the state (unfiltered) -- useful for building a local dataset to "
                              "cross-reference other sources against, without per-address API calls")
    parser.add_argument("--home-type", default=None,
                         help="Redfin's own vocabulary (House, Condo, Townhouse, Multi family, "
                              "Land, Other, Mobile, Co-op per their bylocation docs) -- NOT "
                              "verified on /search/bypolygon specifically, see module docstring "
                              "item 6. Default: omitted, which should mean all types.")
    parser.add_argument("--simplify", choices=["bbox", "hull", "none"], default=SIMPLIFY_METHOD_DEFAULT,
                         help="how to reduce each part's vertex count before sending to Redfin's "
                              "bypolygon (CONFIRMED to reject a real 215-point state polygon -- see "
                              "module docstring). 'bbox' (default): confirmed working, max overshoot. "
                              "'hull': tighter fit, NOT yet tested against this endpoint. 'none': send "
                              "the real boundary as-is -- will likely 404 on any state with a complex "
                              "coastline/border, only useful for small/simple states or once a real "
                              "vertex-count ceiling is confirmed.")
    parser.add_argument("--max-parts", type=int, default=MAX_PARTS_DEFAULT,
                         help=f"max polygon parts to search per state, largest-area first "
                              f"(default {MAX_PARTS_DEFAULT})")
    parser.add_argument("--min-part-area-km2", type=float, default=MIN_PART_AREA_KM2_DEFAULT,
                         help=f"drop any kept part smaller than this (default {MIN_PART_AREA_KM2_DEFAULT} km2)")
    parser.add_argument("--dry-run", action="store_true",
                         help="fetch only page 1 of each selected part to report a result count "
                              "(NOT a reliable total or page count -- see dry_run_polygon() "
                              "docstring, this endpoint has no confirmed 'total' field), then exit "
                              "-- does not write an output file")
    args = parser.parse_args()

    api_key = os.environ.get("REALTYAPI_KEY")
    if not api_key:
        print("ERROR: set the REALTYAPI_KEY environment variable first.", file=sys.stderr)
        sys.exit(1)

    try:
        all_rings = load_state_geometry(args.geojsonl, args.state)
    except (ScriptError, FileNotFoundError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    rings, dropped_by_rank, dropped_by_size = select_parts(
        all_rings, args.max_parts, args.min_part_area_km2)

    print(f"{args.state}: {len(all_rings)} total polygon part(s), searching {len(rings)}")
    if dropped_by_rank:
        print(f"  DROPPED (beyond top {args.max_parts} by area): {[round(a, 1) for a in dropped_by_rank]} km2")
    if dropped_by_size:
        print(f"  DROPPED (< {args.min_part_area_km2} km2): {[round(a, 1) for a in dropped_by_size]} km2")

    # Simplify each part BEFORE any API calls -- this is pure local
    # geometry (shapely/pyproj), costs nothing, and lets us show the real
    # overshoot cost up front so a bad simplification choice is visible
    # before spending quota on it.
    simplified_rings = []
    for part_idx, ring in enumerate(rings):
        try:
            simplified, overshoot = simplify_ring(ring, args.simplify)
        except ScriptError as e:
            print(f"FAILED: {e}", file=sys.stderr)
            sys.exit(1)
        simplified_rings.append(simplified)
        if args.simplify != "none":
            print(f"  part {part_idx + 1}/{len(rings)}: {len(ring)} pts -> {len(simplified)} pts "
                  f"({args.simplify}), overshoot: {overshoot:.2f}x true area "
                  f"({'' if overshoot < 1.5 else 'SIGNIFICANT -- '}searching this much more area than "
                  f"the real state boundary)")
    rings = simplified_rings

    if args.dry_run:
        grand_count = 0
        for part_idx, ring in enumerate(rings):
            part_label = f"part {part_idx + 1}/{len(rings)}"
            try:
                count, pages_lower_bound = dry_run_polygon(ring, api_key, args.home_type,
                                                            foreclosure_only=not args.all_listings)
            except ScriptError as e:
                print(f"FAILED on {part_label}: {e}", file=sys.stderr)
                sys.exit(1)
            more_marker = "+ (at least one more page)" if pages_lower_bound > 1 else ""
            print(f"  [{part_label}] page-1 result count: {count}{more_marker}")
            grand_count += count
        print(f"\nDRY RUN: {grand_count}+ listing(s) seen on page 1 across {len(rings)} part(s) "
              f"-- this is a LOWER BOUND, not a true total (no 'total' field on this endpoint, "
              f"see module docstring item 5). No output file written.")
        return

    all_results: list[dict] = []
    id_field: str | None = None
    id_field_decided = False
    seen_keys: set[str] = set()

    for part_idx, ring in enumerate(rings):
        part_label = f"part {part_idx + 1}/{len(rings)}"
        try:
            part_results = search_polygon(ring, api_key, args.home_type, part_label,
                                           foreclosure_only=not args.all_listings)
        except ScriptError as e:
            print(f"FAILED on {part_label}: {e}", file=sys.stderr)
            sys.exit(1)

        if not id_field_decided and part_results:
            sample_home_data = part_results[0].get("homeData", {})
            id_field = _pick_id_field(sample_home_data)
            id_field_decided = True
            if id_field:
                print(f"  dedupe key: using field '{id_field}' (from homeData) -- CONFIRM this is "
                      f"really a unique listing ID before trusting the dedupe count")
            else:
                print(f"  WARNING: none of {CANDIDATE_ID_FIELDS} found in homeData -- "
                      f"falling back to full-record hashing for dedupe")

        for result in part_results:
            key = _dedupe_key(result, id_field)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_results.append(result)
            print(f"    {_address_summary(result)}")

    suffix = "_all_redfin" if args.all_listings else "_foreclosures_redfin"
    out_path = args.out or f"realtyapi_{args.state.lower()}{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\nDone: {len(all_results)} unique listing(s) across {len(rings)} part(s) -> {out_path}")


if __name__ == "__main__":
    main()