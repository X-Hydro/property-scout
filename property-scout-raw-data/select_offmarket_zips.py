"""
Select zip codes worth running through RealtyAPI's /search/offmarket sweep,
based on active single_family/land listing volume already present in a
realtyapi_bypolygon_state.py output file -- NOT a database query.

WHY FILE-BASED, NOT DB-BASED: an earlier version of this design assumed
listings were already loaded into Postgres before zip selection could run,
which forced the DB load step to happen before this one. That's backwards
for a state like NH, where the goal is a single self-contained
value-loading step per state (matching MA's shape), not a rigid ordering
across separate load commands. Reading directly from the RealtyAPI listings
file -- the same one load_listings_realtyapi.py loads afterward -- removes
that dependency entirely: this can run before, after, or without ever
touching the DB.

Reuses load_listings_realtyapi.py's status/property-type normalization
Reuses realtyapi_response_utils.py's normalization (listing_dict_from_raw,
extract_records) -- a DEPENDENCY-FREE shared module (no DB imports), so
this script never needs listings_db.py just to parse a listings file.
Kept separate from load_listings_realtyapi.py's own copy of the same
logic, to avoid pulling in that file's psycopg2/DB-write dependencies
rather than re-implementing that logic here -- same "one write path"
convention already used elsewhere in this project. In particular this
means flags.is_pending / is_contingent are already handled correctly
(a listing with status="for_sale" but flags.is_pending=true is correctly
excluded here, not miscounted as active) -- same known-stale-status fix
already relied on elsewhere.

Usage:
    python select_offmarket_zips.py realtyapi-data/realtyapi_nh.json \
        --min-count 10 --property-types "Single Family,Land" \
        --out nh_offmarket_zips.txt
"""

import sys
import json
import argparse
from collections import Counter

from realtyapi_response_utils import extract_records, listing_dict_from_raw


def select_zips(records: list[dict], min_count: int, property_types: set[str]):
    """
    Returns (qualifying_zips_sorted, counts_by_zip). A zip qualifies if its
    count of Active listings matching property_types is > min_count
    (strictly greater, per the original ">10" spec -- exactly 10 does not
    qualify).
    """
    counts = Counter()
    skipped_no_zip = 0

    for raw in records:
        row = listing_dict_from_raw(raw)
        if row["status"] != "Active":
            continue
        if row["property_type"] not in property_types:
            continue
        zip_code = row["zip_code"]
        if not zip_code:
            skipped_no_zip += 1
            continue
        counts[zip_code] += 1

    if skipped_no_zip:
        print(f"  NOTE: {skipped_no_zip} matching listing(s) had no zip_code and were skipped")

    qualifying = sorted(z for z, c in counts.items() if c > min_count)
    return qualifying, counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("listings_file",
                         help="realtyapi_bypolygon_state.py output file (bare JSON array, "
                              "or the {message,...,searchResults} wrapper -- both accepted, "
                              "same as load_listings_realtyapi.py)")
    parser.add_argument("--min-count", type=int, default=10,
                         help="zip qualifies if active matching-listing count is > this (default 10)")
    parser.add_argument("--property-types", default="Single Family,Land",
                         help="comma list, using the MAPPED vocabulary (Single Family, Land, "
                              "Condo, etc. -- see PROPERTY_TYPE_MAP in load_listings_realtyapi.py), "
                              "not RealtyAPI's raw snake_case values (default: 'Single Family,Land')")
    parser.add_argument("--out", required=True, help="output path, one qualifying zip per line")
    args = parser.parse_args()

    property_types = set(t.strip() for t in args.property_types.split(","))

    with open(args.listings_file) as f:
        payload = json.load(f)
    records = extract_records(payload)
    print(f"Loaded {len(records)} record(s) from {args.listings_file}")

    qualifying, counts = select_zips(records, args.min_count, property_types)

    with open(args.out, "w") as f:
        for z in qualifying:
            f.write(z + "\n")

    print(f"{len(counts)} distinct zip(s) seen with at least one matching Active listing, "
          f"{len(qualifying)} qualify (> {args.min_count} matching {'/'.join(sorted(property_types))} "
          f"listing(s)) -> {args.out}")

    top = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
    print("\nTop zips by matching listing count:")
    for z, c in top:
        marker = "  (qualifies)" if c > args.min_count else "  (below threshold)"
        print(f"  {z}: {c}{marker}")

    if not qualifying:
        print(f"\nWARNING: no zips qualified at --min-count {args.min_count}. Check the "
              f"--property-types filter matches real values in this file (run "
              f"load_listings_realtyapi.py --inspect on it if unsure), or lower --min-count.")


if __name__ == "__main__":
    main()