"""
RealtyAPI "bypolygon" full-state search.

Looks up a state's real boundary geometry from the Census
cb_2025_us_state_20m.geojsonl file, runs realtor.realtyapi.io's
/search/bypolygon endpoint against it (paginating every page), and
writes all results to a JSON file.

POLYGON FORMAT: confirmed from the working example this replaces --
"lon lat,lon lat,lon lat,..." (comma-separated pairs, space-separated
lon/lat within a pair, no brackets). GeoJSON coordinates are already
stored as [lon, lat], so each ring converts directly with no reordering.
NOT CONFIRMED: whether the API wants the ring closed (first point ==
last point, which is how GeoJSON always stores it) or open. This script
passes the ring through exactly as GeoJSON provides it (closed) --
matches the example's apparent behavior, but worth confirming against a
real multi-page response before trusting it at the full-state scale
this script is meant for.

MULTI-PART STATES, COST-BOUNDED BY DESIGN: Census cartographic boundary
files store many states as MultiPolygon with several disjoint parts
(CONFIRMED from this exact file: 14 states have more than one part, AK
has 47). RealtyAPI charges per request/page, and one full paginated
search PER PART would multiply cost by part count -- unacceptable for
AK's 47 parts. Instead of that, or a single-search convex-hull
approximation (considered and rejected -- see below), this script runs
one search per part for AT MOST THE 5 LARGEST PARTS BY AREA (real
geodesic area via pyproj, not degrees^2), and drops the rest --
accepting incomplete coverage as the deliberate cost/completeness
tradeoff, not something to solve precisely right now.

CONFIRMED IMPACT of this cutoff, checked against every real multi-part
state in this file before deciding to ship it (2026-08-26):
  - Every state with 5 or fewer parts (NY, MA, ME, RI, VA, and by
    definition every other multi-part state in the Northeast/Mid-
    Atlantic) loses NOTHING -- top-5 keeps 100% of their parts.
  - MI and CA each drop exactly one small, low-population-likelihood
    island (161 km2 and 92 km2 respectively).
  - HI drops 3 of 8 parts, most notably a ~350 km2 island (almost
    certainly Lanai) that has real listings, just low volume.
  - AK drops 42 of its 47 parts, several of them real populated-scale
    islands (thousands of km2, e.g. the Aleutian chain) -- a genuine,
    known, ACCEPTED gap. AK is explicitly out of scope for now (per
    2026-08-26 conversation) -- if AK ever needs real coverage, this
    cutoff is not sufficient and needs its own state-specific handling,
    not a blanket constant.

MIN_PART_AREA_KM2 (currently a NO-OP on this file, kept anyway): every
part in this file is already above 10 km2 -- Census's own cartographic
generalization already drops genuinely tiny slivers before this file is
generated, so the smallest real part seen anywhere (NY, 37.6 km2) is
still well above the threshold. This constant is defensive/future-
proofing in case a higher-resolution boundary file is ever substituted
(one with real sub-10km2 debris), not something doing real work today
-- don't assume it's pruning anything from THIS file.

REJECTED ALTERNATIVE, for the record: a single convex-hull search
covering all parts in one request. Checked against real geometry -- for
compact states the hull only overshoots true area by 1.2-1.7x (VT,
MA, CA), which would've been fine, but AK's hull overshoots by 13.6x
because the Aleutian chain stretches the hull across huge stretches of
open ocean, and RealtyAPI's own docs confirm the server-side polygon
filter matches whatever ring you send -- not the true state shape --
so an oversized hull risks actually billing for out-of-boundary
results, not just fetching harmlessly-empty ocean. The top-N-by-area
approach avoids that risk entirely: every search that runs is against a
real, exact part boundary, never an approximation that could billed-
overfetch into a neighboring state.

DEDUPE ACROSS PARTS: a listing near a part boundary could plausibly be
returned by more than one part's search. Deduping requires a real
per-listing unique ID field, and the actual RealtyAPI listing schema
for /search/bypolygon hasn't been confirmed field-by-field in this
project (only the top-level wrapper -- message/source/total/nextPage/
resultCount/searchResults -- has been seen). This script tries a short
list of plausible ID field names on the FIRST result it sees and uses
whichever one is actually present, printing which one it picked -- run
once against a real multi-part state and confirm the printed field name
is really a unique listing ID (not e.g. an MLS office ID) before
trusting the dedupe count. If NONE of the candidates are present, dedup
falls back to a hash of the full record, which is strictly weaker --
this fallback prints a loud warning rather than silently under-deduping.

URL LENGTH: GET request, polygon coordinates go in the query string.
CONFIRMED risk, not yet hit in practice: CA's largest part alone is 412
points, at ~20 chars per "lon lat," pair that's roughly 8KB of query
string, which may exceed a server or proxy's URL length limit. This
script does not work around that (e.g. by switching to POST) since it's
unconfirmed whether the API even accepts POST for this endpoint -- if a
large state fails with a 414/431 or a connection-level error, that's
the likely cause; check whether the API has a POST variant before doing
anything more elaborate.

GEOJSONL PATH: d:/data/us_state_boundaries/cp_2025_us_state_20m.geojsonl
(hardcoded as the default below, matching what's already on disk --
override with --geojsonl if it moves).

Usage:
    pip install pyproj
    set REALTYAPI_KEY=...          (Windows)
    export REALTYAPI_KEY=...       (bash)
    python realtyapi_bypolygon_state.py VT
    python realtyapi_bypolygon_state.py CA --property-type single_family,land --out ca_listings.json
    python realtyapi_bypolygon_state.py HI --max-parts 8   # override the default cap for a state you know needs it
"""

import argparse
import hashlib
import json
import os
import sys
import time

import requests
from pyproj import Geod

API_URL = "https://realtor.realtyapi.io/search/bypolygon"
GEOJSONL_PATH_DEFAULT = "d:/data/us_state_boundaries/cp_2025_us_state_20m.geojsonl"
PAGE_SLEEP_SECONDS = 0.5
MAX_RETRIES = 3
MAX_PARTS_DEFAULT = 5
MIN_PART_AREA_KM2_DEFAULT = 10.0  # currently a no-op on this file -- see module docstring

# Hardcoded rather than CLI flags -- none of these have a real reason to
# vary per run. RESULT_COUNT in particular is pinned at the API's max
# (200): a smaller page size only means MORE requests for the same
# data, which fights the whole reason --max-parts exists. If one of
# these genuinely needs to change later, edit the constant here rather
# than re-adding a flag nobody will remember to use.
SEARCH_TYPE = "For_Sale"
SORT_ORDER = "Recommended"
RESULT_COUNT = 200

_GEOD = Geod(ellps="WGS84")

# Tried in this order against the first result seen; whichever is
# actually present wins. See "DEDUPE ACROSS PARTS" in the module
# docstring -- none of these are confirmed real RealtyAPI field names,
# they're plausible guesses to try against real data.
CANDIDATE_ID_FIELDS = ["id", "propertyId", "property_id", "listingId", "listing_id", "mlsId", "mls_id"]


class ScriptError(Exception):
    pass


def _ring_area_km2(ring: list[list[float]]) -> float:
    """Real geodesic area (WGS84), not shapely's raw degrees^2 -- the
    two disagree enough at US latitudes to matter for a km2 threshold."""
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    area, _ = _GEOD.polygon_area_perimeter(lons, lats)
    return abs(area) / 1e6


def load_state_geometry(geojsonl_path: str, state_code: str) -> list[list[list[float]]]:
    """Returns a list of rings (each a list of [lon, lat] points), one
    per polygon part -- flattened from either a Polygon (1 part) or
    MultiPolygon (N parts) feature. Raises if the state isn't found, or
    if any part has more than one ring (a hole this script doesn't
    handle -- see module docstring)."""
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
                        f"single-ring parts, see module docstring"
                    )
                rings.append(part[0])
            return rings

    raise ScriptError(f"No feature with STUSPS == '{state_code}' found in {geojsonl_path}")


def select_parts(rings: list[list[list[float]]], max_parts: int,
                  min_area_km2: float) -> tuple[list[list[list[float]]], list[float], list[float]]:
    """Ranks parts by real area (largest first), keeps at most max_parts,
    and drops any of those under min_area_km2. Returns
    (kept_rings, dropped_by_rank_areas, dropped_by_size_areas) so the
    caller can print exactly what got left out, rather than dropping it
    silently -- see module docstring's CONFIRMED IMPACT section for why
    this matters (AK/HI lose real, listable islands under this rule)."""
    with_area = sorted(((r, _ring_area_km2(r)) for r in rings), key=lambda x: -x[1])
    top = with_area[:max_parts]
    dropped_by_rank = [a for _, a in with_area[max_parts:]]

    kept = [(r, a) for r, a in top if a >= min_area_km2]
    dropped_by_size = [a for _, a in top if a < min_area_km2]

    return [r for r, _ in kept], dropped_by_rank, dropped_by_size


def ring_to_polygon_param(ring: list[list[float]]) -> str:
    """[lon, lat] points -> "lon lat,lon lat,..." -- confirmed format,
    see module docstring. Passed through exactly as GeoJSON provides it
    (closed ring, first point == last point)."""
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


def _pick_id_field(sample_result: dict) -> str | None:
    for field in CANDIDATE_ID_FIELDS:
        if field in sample_result:
            return field
    return None


def _dedupe_key(result: dict, id_field: str | None) -> str:
    if id_field is not None:
        return f"{id_field}:{result.get(id_field)}"
    # Fallback: hash the whole record. Weaker -- see module docstring.
    return "hash:" + hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def dry_run_polygon(ring: list[list[float]], api_key: str, property_type: str | None,
                     foreclosure: bool = False) -> tuple[int, int]:
    """Fetches ONLY page 1 (one request, not the full pagination) and
    reads the API's own "total" field to compute (total_listings,
    pages_needed) for this part. This is the entire cost model a dry
    run needs -- RealtyAPI reports the true total up front, so there's
    no need to actually walk every page just to count them."""
    headers = {"x-realtyapi-key": api_key}
    params = {
        "polygon": ring_to_polygon_param(ring),
        "page": 1,
        "resultCount": RESULT_COUNT,
        "sortOrder": SORT_ORDER,
        "searchType": SEARCH_TYPE,
    }
    if property_type:
        params["propertyType"] = property_type
    # CONFIRMED working on /search/bypolygon (2026-09-06): tested against a
    # box drawn to genuinely contain a known is_foreclosure:true listing
    # (9186 Steel St, Detroit MI 48228) -- an earlier test with a box that
    # missed the point by ~0.03 degrees of latitude wrongly looked like a
    # broken filter. Once the box actually contained the point, the same
    # filter correctly returned it (plus 2 more real foreclosure-flagged
    # listings). Lesson: always verify polygon coverage against a known
    # point before trusting a zero-result test as evidence of a bug.
    if foreclosure:
        params["foreclosure"] = "true"
    data = _request_with_retries(headers, params)
    total = data.get("total", 0)
    pages = -(-total // RESULT_COUNT) if total else 0  # ceil division
    return total, pages


def search_polygon(ring: list[list[float]], api_key: str, property_type: str | None,
                    part_label: str, foreclosure: bool = False) -> list[dict]:
    headers = {"x-realtyapi-key": api_key}
    params = {
        "polygon": ring_to_polygon_param(ring),
        "page": 1,
        "resultCount": RESULT_COUNT,
        "sortOrder": SORT_ORDER,
        "searchType": SEARCH_TYPE,
    }
    if property_type:
        params["propertyType"] = property_type
    if foreclosure:
        params["foreclosure"] = "true"
    part_results = []
    last_known_total = None
    while True:
        data = _request_with_retries(headers, params)
        results = data.get("searchResults", [])
        part_results.extend(results)
        reported_total = data.get("total")
        print(f"  [{part_label}] page {params['page']}: {len(results)} listings, "
              f"{len(part_results)}/{reported_total}")
        if reported_total:
            last_known_total = reported_total
        if not data.get("nextPage"):
            break
        params["page"] += 1
        time.sleep(PAGE_SLEEP_SECONDS)

    # CONFIRMED real failure mode (MD, 2026-08-26): the API silently caps
    # pagination at 10,000 results (50 pages x 200/page -- almost
    # certainly an Elasticsearch-style max_result_window default on
    # RealtyAPI's backend, not something documented up front). Page 50
    # reported total=16210 with more data clearly available; page 51
    # returned 0 results AND total=0, which makes nextPage falsy and
    # ends the loop as if the search were genuinely exhausted. Without
    # this check, that looks identical to a normal, complete run --
    # ~38% of MD's real listings were silently dropped with no error and
    # no warning in the original version of this function. Comparing
    # what we actually fetched against the LAST total the API reported
    # before it (suspiciously) dropped to 0 catches this instead of
    # reporting a false "Done".
    if last_known_total is not None and len(part_results) < last_known_total:
        print(f"  WARNING [{part_label}]: fetched {len(part_results)} but the API last "
              f"reported total={last_known_total} before returning an empty/zero-total page "
              f"-- this looks like a pagination cap (commonly 10,000 results), NOT a genuine "
              f"end of results. This part's data is INCOMPLETE. See module docstring's "
              f"CONFIRMED failure mode notes for how to work around this (splitting the query "
              f"into smaller sub-searches) before trusting this part's output.")

    return part_results


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""example:
  python realtyapi_bypolygon_state.py VT --geojsonl d:/data/us_state_boundaries/cp_2025_us_state_20m.geojsonl
""",
    )
    parser.add_argument("state", help="2-letter state abbreviation, e.g. VT")
    parser.add_argument("--geojsonl", default=GEOJSONL_PATH_DEFAULT,
                         help=f"path to the .geojsonl file, one Feature per line "
                              f"(default: {GEOJSONL_PATH_DEFAULT})")
    parser.add_argument("--out", help="output JSON path (default: realtyapi_<state>.json)")
    parser.add_argument("--property-type", default=None,
                         help="comma list, e.g. single_family,land (see RealtyAPI's Realtor.com "
                              "docs for accepted values -- not verified in this project). "
                              "Default: omitted entirely, which should return all property types, "
                              "since this is documented as an optional filter.")
    parser.add_argument("--foreclosure", action="store_true",
                         help="only return MLS-listed foreclosures (adds foreclosure=true). "
                              "CONFIRMED working on /search/bypolygon as of 2026-09-06 -- see "
                              "the comment in dry_run_polygon() for how that was verified")
    parser.add_argument("--max-parts", type=int, default=MAX_PARTS_DEFAULT,
                         help=f"max polygon parts to search per state, largest-area first "
                              f"(default {MAX_PARTS_DEFAULT}) -- see module docstring for the "
                              f"confirmed coverage impact per state, especially AK/HI")
    parser.add_argument("--min-part-area-km2", type=float, default=MIN_PART_AREA_KM2_DEFAULT,
                         help=f"drop any kept part smaller than this (default "
                              f"{MIN_PART_AREA_KM2_DEFAULT} km2) -- currently a no-op on the "
                              f"cb_2025_us_state_20m.geojsonl file, see module docstring")
    parser.add_argument("--dry-run", action="store_true",
                         help="fetch only page 1 of each selected part (up to --max-parts "
                              "requests total) to report total listings and total pages "
                              "needed, then exit -- does not write an output file")
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
        print(f"  DROPPED (beyond top {args.max_parts} by area): "
              f"{[round(a, 1) for a in dropped_by_rank]} km2 -- "
              f"see module docstring's CONFIRMED IMPACT notes before assuming this is harmless")
    if dropped_by_size:
        print(f"  DROPPED (< {args.min_part_area_km2} km2): {[round(a, 1) for a in dropped_by_size]} km2")

    if args.dry_run:
        grand_total = 0
        grand_pages = 0
        for part_idx, ring in enumerate(rings):
            part_label = f"part {part_idx + 1}/{len(rings)}"
            try:
                total, pages = dry_run_polygon(ring, api_key, args.property_type, args.foreclosure)
            except ScriptError as e:
                print(f"FAILED on {part_label}: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"  [{part_label}] total listings: {total}  ->  pages needed: {pages}")
            grand_total += total
            grand_pages += pages
        print(f"\nDRY RUN: {grand_total} total listing(s) across {len(rings)} part(s), "
              f"{grand_pages} page(s)/request(s) for the full fetch "
              f"(plus {len(rings)} already spent on this dry run). No output file written.")
        return

    all_results: list[dict] = []
    id_field: str | None = None
    id_field_decided = False
    seen_keys: set[str] = set()

    for part_idx, ring in enumerate(rings):
        part_label = f"part {part_idx + 1}/{len(rings)}"
        try:
            part_results = search_polygon(ring, api_key, args.property_type, part_label, args.foreclosure)
        except ScriptError as e:
            print(f"FAILED on {part_label}: {e}", file=sys.stderr)
            sys.exit(1)

        if not id_field_decided and part_results:
            id_field = _pick_id_field(part_results[0])
            id_field_decided = True
            if id_field:
                print(f"  dedupe key: using field '{id_field}' (first match from "
                      f"{CANDIDATE_ID_FIELDS}) -- CONFIRM this is really a unique "
                      f"listing ID before trusting the dedupe count, see module docstring")
            else:
                print(f"  WARNING: none of {CANDIDATE_ID_FIELDS} found in a real result -- "
                      f"falling back to full-record hashing for dedupe, which under-catches "
                      f"near-duplicate records, see module docstring")

        for result in part_results:
            key = _dedupe_key(result, id_field)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_results.append(result)

    suffix = "_foreclosures" if args.foreclosure else ""
    out_path = args.out or f"realtyapi_{args.state.lower()}{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"Done: {len(all_results)} unique listing(s) across {len(rings)} part(s) -> {out_path}")


if __name__ == "__main__":
    main()