"""
NH VGSI gap-fill — ValueGap

Fills in assessed values for parcels the offmarket point-in-polygon join
(join_parcels_offmarket.py) left with no value, using VGSI's targeted
address lookup (vgsi_targeted_lookup.py) instead of a full town scan.

SCOPED TO WHAT GAP ANALYSIS CAN ACTUALLY USE (Thale, 2026-09): gap
analysis only ever compares a listing's price against nearby comps'
values within ~200m (same constraint nh_spider.py's SEED_RADIUS_MILES
comment already documents for the offmarket seed grid). A value on a
parcel with no listing within that radius can never be used by gap
analysis -- gap-filling it would spend a real VGSI request (against a
small public-sector site this project has committed to treating as a
polite, low-volume citizen) for a value that helps nothing. So every
candidate parcel is checked against MAX_GAPFILL_DISTANCE_M BEFORE any
VGSI request is attempted, not after.

ALSO CHECKS has_vgsi_coverage() ONCE PER TOWN before touching any of its
parcels -- VGSI is not NH's statewide assessor platform (see
vgsi_targeted_lookup.py). A town not on that list gets skipped entirely,
instantly, with zero requests -- not discovered one per-address timeout
at a time (see: Freedom).

INPUT: the normalized per-town files load_property_values.py actually
loads (e.g. nh_data_statewide/nh_freedom.geojson) -- NOT the intermediate
*_offmarket_joined.geojson. The normalized file already has everything
needed in one place: property_id, address (StreetAddress fallback for
anything unmatched), latitude/longitude (parcel centroid), municipality,
and assessed_value (None = gap-fill candidate).

OUTPUT: rewrites each input file in place (atomic: tmp + os.replace),
filling total_market_value/source/etc. on whatever parcels resolved.
Re-run load_property_values.py against the same files afterward to push
the fills into Postgres -- this script never touches the DB directly.

Usage:
    python nh_vgsi_gapfill.py nh_data_statewide/nh_*.geojson \\
        --listings-file realtyapi-data/realtyapi_nh.json

    python nh_vgsi_gapfill.py nh_data_statewide/nh_freedom.geojson \\
        --listings-file realtyapi-data/realtyapi_nh.json --dry-run
"""

import re
import sys
import os
import json
import glob
import argparse
import concurrent.futures
from pathlib import Path
from datetime import date
from math import radians, sin, cos, sqrt, atan2

PIPELINE_DIR = Path(__file__).parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))
from vgsi_targeted_lookup import lookup_and_fetch, has_vgsi_coverage, guess_town_slug

MAX_GAPFILL_DISTANCE_M = 200.0  # see module docstring -- gap analysis' own comp radius

# Politeness/concurrency convention matched to the old vgsi_assessment_scraper.py
# (VGSI is a small public-sector site, no documented rate limit, this project
# has already committed to being a low-volume polite citizen).
MAX_WORKERS = 5


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters. Good enough at NH's scale --
    not worth pulling in a projected-CRS dependency for a 200m threshold
    check."""
    R = 6371000.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def _load_town_listing_points(town: str, listings_file: str) -> list[tuple[float, float]]:
    """Coordinates of every active listing in `town`, from the statewide
    listings file. Standalone rather than reusing NHSpider's own
    _load_town_listing_points -- this script shouldn't need REALTYAPI_KEY
    or any offmarket-sweep setup just to check listing proximity for a
    VGSI-only operation."""
    with open(listings_file) as f:
        payload = json.load(f)
    records = payload if isinstance(payload, list) else payload.get("searchResults", [])
    return [
        (r["address"]["latitude"], r["address"]["longitude"])
        for r in records
        if (r.get("address", {}).get("city") or "").lower() == town.lower()
        and r.get("address", {}).get("latitude") is not None
    ]


def _nearest_listing_distance_m(lat, lon, listing_points) -> float | None:
    if lat is None or lon is None or not listing_points:
        return None
    return min(_haversine_m(lat, lon, llat, llon) for llat, llon in listing_points)


_HOUSE_NUMBER_RE = re.compile(r"^\s*\d")


def _has_house_number(address: str) -> bool:
    """True if `address` starts with a number (e.g. '170 South Peak
    Road'). A bare street name with no house number (e.g. 'SOUTH PEAK
    ROAD') can match several real parcels on VGSI's address search --
    CONFIRMED (2026-09, Lincoln pilot run): two different Lincoln
    parcels, both missing a house number in GRANIT's StreetAddress, both
    resolved as 'ambiguous' against the identical 5-candidate list and
    were BOTH silently assigned the same $1,347,600 value -- at most one
    of them can be correct. Skipping these before ever calling VGSI is
    cheaper and more honest than attempting the lookup and hoping the
    ambiguous-match fallback happens to be right."""
    return bool(_HOUSE_NUMBER_RE.match(address or ""))


def gapfill_file(path: str, listings_file: str, max_distance_m: float,
                  dry_run: bool = False) -> dict:
    """Processes one town's normalized geojson in place. Returns a dict
    of summary counts for this file."""
    with open(path) as f:
        fc = json.load(f)
    features = fc.get("features", [])
    if not features:
        print(f"  {path}: 0 features, skipping")
        return {}

    town = features[0].get("properties", {}).get("municipality")
    if not town:
        print(f"  WARNING: {path}: no municipality on first feature, can't determine town -- skipping file")
        return {}

    counts = {"filled": 0, "candidates": 0, "already_valued": 0, "too_far": 0, "no_listings_in_town": 0,
              "not_covered": 0, "no_match": 0, "ambiguous": 0, "failed": 0, "no_address": 0,
              "no_house_number": 0}

    if not has_vgsi_coverage(town):
        print(f"  {path}: {town} has no VGSI coverage -- skipping entire file (0 requests)")
        counts["not_covered"] = sum(
            1 for feat in features if feat.get("properties", {}).get("assessed_value") is None
        )
        return counts

    listing_points = _load_town_listing_points(town, listings_file)
    if not listing_points:
        print(f"  {path}: {town} has no active listings in the listings file -- nothing within "
              f"{max_distance_m:.0f}m of anything, skipping entire file (0 requests)")
        counts["no_listings_in_town"] = sum(
            1 for feat in features if feat.get("properties", {}).get("assessed_value") is None
        )
        return counts

    town_slug = guess_town_slug(town)

    # Cheap, local filtering pass first -- distance check costs nothing and
    # runs before any network request is even considered.
    candidates = []  # (feature, address) pairs actually worth a VGSI request
    for feature in features:
        props = feature["properties"]
        if props.get("assessed_value") is not None:
            counts["already_valued"] += 1
            continue

        address = props.get("address")
        if not address or not address.strip():
            counts["no_address"] += 1
            continue

        if not _has_house_number(address):
            counts["no_house_number"] += 1
            continue

        dist = _nearest_listing_distance_m(props.get("latitude"), props.get("longitude"), listing_points)
        if dist is None or dist > max_distance_m:
            counts["too_far"] += 1
            continue

        candidates.append((feature, address))

    print(f"  {path}: {town} ({town_slug}) -- {len(candidates)} candidate(s) within "
          f"{max_distance_m:.0f}m of a listing, {counts['already_valued']} already valued, "
          f"{counts['too_far']} too far, {counts['no_address']} no address, "
          f"{counts['no_house_number']} no house number (skipped, too ambiguous to trust)")
    counts["candidates"] = len(candidates)

    if not candidates:
        return counts

    if dry_run:
        print(f"  [dry-run] would attempt {len(candidates)} VGSI lookup(s) for {town} -- no requests made")
        return counts

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(lookup_and_fetch, town_slug, address): (feature, address)
            for feature, address in candidates
        }
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            feature, address = futures[future]
            parsed, status = future.result()
            props = feature["properties"]

            if status == "no_match":
                counts["no_match"] += 1
            elif status and status.startswith("ambiguous"):
                counts["ambiguous"] += 1
            elif status and (status.startswith("search_failed") or status.startswith("fetch_failed")):
                counts["failed"] += 1
                print(f"  [{i}/{len(candidates)}] {address}: {status}")
                continue

            if parsed and parsed.get("total_market_value") is not None:
                props["assessed_value"] = float(parsed["total_market_value"])
                props["property_type"] = parsed.get("land_use_desc") or props.get("property_type")
                props["source"] = "NH_VGSI_TARGETED"
                props["source_date"] = date.today().isoformat()
                counts["filled"] += 1
                print(f"  [{i}/{len(candidates)}] {address}: filled (${parsed['total_market_value']}, {status})")
            elif status == "ok":
                # Resolved a parcel but it had no total_market_value itself
                # (e.g. old-layout page missing the field) -- not counted as
                # filled, not an error either. Leave assessed_value null.
                print(f"  [{i}/{len(candidates)}] {address}: matched but no value on the page ({status})")

    tmp_path = path + ".partial"
    with open(tmp_path, "w") as f:
        json.dump(fc, f)
    os.replace(tmp_path, path)

    return counts


def _expand_paths(paths: list[str]) -> list[str]:
    expanded = []
    for p in paths:
        matches = sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
        for m in matches:
            if os.path.isfile(m):
                expanded.append(m)
            else:
                print(f"  WARNING: {m} does not exist -- skipped")
    return expanded


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="normalized per-town geojson file(s), e.g. "
                                                    "nh_data_statewide/nh_*.geojson (shell-expanded "
                                                    "globs work too)")
    parser.add_argument("--listings-file", required=True,
                         help="statewide RealtyAPI listings file, e.g. realtyapi-data/realtyapi_nh.json")
    parser.add_argument("--max-distance-m", type=float, default=MAX_GAPFILL_DISTANCE_M,
                         help=f"only gap-fill parcels within this distance of an active listing "
                              f"(default {MAX_GAPFILL_DISTANCE_M:.0f}m, matching gap analysis' own "
                              f"comp radius -- a value farther than this can never be used)")
    parser.add_argument("--dry-run", action="store_true",
                         help="show what would be attempted per town without making any VGSI requests")
    args = parser.parse_args()

    paths = _expand_paths(args.files)
    if not paths:
        print("ERROR: no files found.")
        sys.exit(1)

    print(f"Gap-filling {len(paths)} file(s), max distance {args.max_distance_m:.0f}m"
          + (" [DRY RUN]" if args.dry_run else "") + "...\n")

    grand = {}
    for path in paths:
        counts = gapfill_file(path, args.listings_file, args.max_distance_m, dry_run=args.dry_run)
        for k, v in counts.items():
            grand[k] = grand.get(k, 0) + v

    print("\n" + "=" * 60)
    print("DONE.")
    for k, v in grand.items():
        print(f"  {k}: {v}")
    print("=" * 60)
    if not args.dry_run and grand.get("filled"):
        print(f"\n{grand['filled']} parcel(s) filled -- re-run load_property_values.py against "
              f"the same file(s) to push these into Postgres.")


if __name__ == "__main__":
    main()