"""
Statewide RI offmarket value sweep -- REWRITTEN, two phases, replacing
the per-TownCode loop this file originally had.

WHY THE REWRITE: the original per-town design gave each TownCode its
own fresh, empty visited_cells/seen_zpids set (see ri_spider.py's
_sweep_offmarket_for_town()). seed_grid() (offmarket_value_sweep.py)
snaps listings to an ABSOLUTE global lattice, so two neighboring towns
CAN and DO independently compute the same cell_key for shared border
territory -- but with no shared cache, neither town's run knows the
other already paid for that cell. Confirmed directly from a real run:
LC (3,503 parcels) returned 4,996 unique offmarket records, a ratio
that only makes sense if LC's sweep pulled in real neighboring-town
area. RI's towns are small enough relative to SEED_RADIUS_MILES=2.0
(inherited unchanged from NH) that this happens A LOT -- confirmed
root cause of "roughly 2K calls and climbing" with 21/39 towns still
to go.

FIX: sweep ONCE, statewide, with ONE shared, disk-persisted cache
(--cache, default ri_offmarket_cache.json) tracking every visited cell
and every seen zpid across the ENTIRE state, not per town. A cell
anywhere in RI can only ever be charged for once, no matter how many
towns' geography it happens to straddle. This matches RealtyAPI
support's own description of the right approach: "de-duplicate on
zpid... once you have the base set, refreshing is cheaper since you
can re-run only the cells you care about."

PHASE 1 -- sweep (spends REALTYAPI_KEY budget, resumable):
    python sweep_ri_statewide.py sweep --max-session-calls 1800 \
        --listings-file ./ri_data/realtyapi_ri.json

    Seeds ONE grid from EVERY listing statewide (not per-town-bbox-
    filtered), sweeps whichever seed cells aren't already in the cache,
    saves the cache after every cell (crash-safe, same reasoning the
    old per-town checkpoint used, just at cell granularity now). Run
    this again next month with the same command -- already-visited
    cells are skipped for free, only genuinely new cells cost calls.

PHASE 2 -- join (ZERO API calls, safe to rerun anytime):
    python sweep_ri_statewide.py join --out ./ri_data_statewide

    Point-in-polygon joins whatever's in the cache so far against
    EVERY TownCode's parcels (RIDEM fetch is free/unlimited, unlike
    RealtyAPI), writing the same per-town output geojson the original
    version produced. Safe to run against a still-partial cache --
    unmatched parcels just get null value fields (existing behavior,
    unchanged) -- and cheap enough to rerun after every sweep session
    to refresh all 39 towns' output, not just newly-swept ones.

MIGRATING PRIOR SPEND: your existing per-TownCode runs (ri_data/,
ri_data_statewide/offmarket-raw/*/) already spent real API calls under
the OLD architecture. Their unique records are worth keeping --
`migrate` folds their zpids into the new cache's seen_zpids/records so
phase 2 (join) can use them immediately. IMPORTANT CAVEAT: this can
only migrate RESULTS, not which physical cells were already covered --
the old per-town raw files never recorded their own query cells, only
the final deduped records. So already-swept geography WILL still look
"new" to phase 1's visited_cells check and may get re-swept once more
this run -- that portion of prior spend is genuinely sunk cost, not
recoverable. Going forward, nothing will double-spend again.

    python sweep_ri_statewide.py migrate --scan-dir ./ri_data_statewide/offmarket-raw
    python sweep_ri_statewide.py migrate --scan-dir ./ri_data/offmarket-raw
"""

import sys
import os
import json
import glob
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent  # spiders/ri/ -> spiders/ -> project root
sys.path.insert(0, str(PROJECT_ROOT))
import offmarket_value_sweep
import join_parcels_offmarket
from spiders.ri.ri_spider import RISpider

DEFAULT_CACHE = "ri_offmarket_cache.json"


def load_cache(path: str) -> dict:
    if not os.path.exists(path):
        return {"visited_cells": set(), "seen_zpids": set(), "records": []}
    with open(path) as f:
        data = json.load(f)
    return {
        "visited_cells": {tuple(c) for c in data.get("visited_cells", [])},
        "seen_zpids": set(data.get("seen_zpids", [])),
        "records": data.get("records", []),
    }


def save_cache(path: str, cache: dict):
    tmp_path = path + ".partial"
    with open(tmp_path, "w") as f:
        json.dump({
            "visited_cells": [list(c) for c in cache["visited_cells"]],
            "seen_zpids": list(cache["seen_zpids"]),
            "records": cache["records"],
        }, f)
    os.replace(tmp_path, path)  # atomic, same convention as base.py's other writes


def load_all_listing_points(listings_file: str) -> list:
    with open(listings_file) as f:
        payload = json.load(f)
    records = payload if isinstance(payload, list) else payload.get("searchResults", [])
    return [
        (r["address"]["latitude"], r["address"]["longitude"])
        for r in records
        if (r.get("address") or {}).get("latitude") is not None
    ]


def cmd_sweep(args):
    api_key = os.environ.get("REALTYAPI_KEY")
    if not api_key:
        print("ERROR: REALTYAPI_KEY not set in the environment.")
        sys.exit(1)

    listings_file = args.listings_file or str(PROJECT_ROOT / "realtyapi-data" / "realtyapi_ri.json")
    if not os.path.exists(listings_file):
        print(f"ERROR: listings file not found at {listings_file} -- run "
              f"realtyapi_bypolygon_state.py RI first, or pass --listings-file.")
        sys.exit(1)

    cache = load_cache(args.cache)
    points = load_all_listing_points(listings_file)
    seeds = offmarket_value_sweep.seed_grid(points, args.seed_radius)
    already_visited = len(cache["visited_cells"])
    print(f"{len(points)} statewide listing(s) -> {len(seeds)} total seed cell(s) at "
          f"{args.seed_radius}mi ({already_visited} already swept in a prior session, "
          f"cache: {args.cache})\n")

    call_counter = [0]
    incomplete_areas = []
    new_cells_this_session = 0

    for i, (lat, lon) in enumerate(seeds, 1):
        cell_key = (round(lat, 4), round(lon, 4), round(args.seed_radius, 5))
        if cell_key in cache["visited_cells"]:
            continue  # already covered -- possibly from a DIFFERENT town's geography overlapping here
        if call_counter[0] >= args.max_session_calls:
            remaining = len(seeds) - i - new_cells_this_session
            print(f"\nSTOPPING: {call_counter[0]} call(s) used this session, "
                  f"--max-session-calls {args.max_session_calls} reached. "
                  f"Approximately {remaining} seed cell(s) remain for next run "
                  f"(recursive splitting means the real remaining count may differ).")
            break
        new_cells_this_session += 1
        print(f"=== seed {i}/{len(seeds)} (new cell {new_cells_this_session}) ===")
        offmarket_value_sweep.sweep_complete(
            lat, lon, args.seed_radius, args.min_radius, api_key,
            cache["seen_zpids"], cache["records"], incomplete_areas, call_counter,
            args.max_session_calls, cache["visited_cells"]
        )
        save_cache(args.cache, cache)  # after EVERY cell -- same crash-safety reasoning as the old checkpoint

    print(f"\n{'=' * 60}")
    print(f"SESSION DONE: {call_counter[0]} call(s) used, {new_cells_this_session} new cell(s) swept")
    print(f"Cache now holds {len(cache['records'])} unique statewide record(s) across "
          f"{len(cache['visited_cells'])} swept cell(s) total")
    if incomplete_areas:
        print(f"{len(incomplete_areas)} area(s) not confirmed complete (budget-limited within a cell's "
              f"own recursive split):")
        for lat, lon, rad, reason in incomplete_areas:
            print(f"  {reason} @ ({lat:.5f}, {lon:.5f}), radius {rad}mi")
    print(f"{'=' * 60}")


def cmd_join(args):
    cache = load_cache(args.cache)
    if not cache["records"]:
        print(f"Cache at {args.cache} is empty -- run the 'sweep' phase first.")
        return

    # join_parcels_offmarket.load_offmarket_points() expects a directory of
    # raw sweep JSON file(s) -- write the cache out once, in that shape,
    # reused for every town's join below.
    tmp_dir = Path(args.out) / "_statewide_offmarket_cache"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with open(tmp_dir / "statewide_offmarket_raw.json", "w") as f:
        json.dump({"offMarketResults": cache["records"]}, f)

    points = join_parcels_offmarket.load_offmarket_points(str(tmp_dir))
    tree = join_parcels_offmarket.build_index(points)
    print(f"Joining against {len(cache['records'])} cached statewide offmarket point(s)...\n")

    spider = RISpider(out_dir=args.out, value_source="dem")  # "dem" here only controls fetch_town()'s
                                                               # branch, which this phase bypasses entirely --
                                                               # _normalize_feature() below detects real
                                                               # value fields by their actual presence, not
                                                               # by this flag (see ri_spider.py fix)
    codes = spider.list_towns()
    failed_codes = []
    for code in codes:
        print(f"[{code}] fetching parcels + joining...")
        try:
            # _retry() -- same wrapper StateSpider.run() already uses for
            # fetch_town(). Calling _get_raw_parcels_geojson() directly
            # (as this did originally) bypassed it entirely, so a
            # transient reset (WinError 10054, same class of error
            # base.py's retry/backoff already handles fine elsewhere,
            # e.g. SK/WK during the original sweep runs) crashed this
            # whole 39-town loop instead of retrying and continuing.
            raw_path = spider._retry(spider._get_raw_parcels_geojson, code)
            joined_path = Path(args.out) / f"{code.lower()}_ri_offmarket_joined.geojson"
            join_parcels_offmarket.join_town_file(raw_path, tree, points, str(joined_path))

            with open(joined_path) as f:
                joined = json.load(f)
            records = [spider._normalize_feature(feat, code) for feat in joined["features"]]
            spider._validate_records(records, code)
            out_path = Path(args.out) / f"ri_{code.lower()}.geojson"
            spider._write_geojson(records, out_path)
            n_valued = sum(1 for r in records if r.get("assessed_value") is not None)
            print(f"  {len(records)} record(s), {n_valued} with a matched value -> {out_path}")
        except Exception as e:
            print(f"  FAILED: {code}: {e}")
            failed_codes.append((code, str(e)))

    if failed_codes:
        print(f"\n{len(failed_codes)} town(s) failed after retries -- NOT written, rerun join to retry "
              f"just these (already-completed towns above are untouched by a rerun):")
        for code, err in failed_codes:
            print(f"  {code}: {err}")


def cmd_migrate(args):
    """Folds prior per-TownCode runs' unique records into the shared
    cache -- see module docstring's MIGRATING PRIOR SPEND section for
    what this can and can't recover."""
    cache = load_cache(args.cache)
    before = len(cache["records"])
    raw_files = glob.glob(os.path.join(args.scan_dir, "*", "*_offmarket_raw.json"))
    if not raw_files:
        print(f"No *_offmarket_raw.json files found under {args.scan_dir}")
        return
    for path in raw_files:
        with open(path) as f:
            payload = json.load(f)
        for r in payload.get("offMarketResults", []):
            zpid = r.get("zpid")
            if zpid is not None and zpid not in cache["seen_zpids"]:
                cache["seen_zpids"].add(zpid)
                cache["records"].append(r)
    save_cache(args.cache, cache)
    print(f"Migrated {len(raw_files)} file(s): {before} -> {len(cache['records'])} unique record(s) in {args.cache}")
    print("NOTE: visited_cells was NOT updated by this -- see module docstring, prior spend on "
          "already-swept geography is not recoverable, only the resulting records are.")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="phase", required=True)

    p_sweep = sub.add_parser("sweep", help="spend REALTYAPI_KEY budget, resumable")
    p_sweep.add_argument("--cache", default=DEFAULT_CACHE)
    p_sweep.add_argument("--listings-file", default=None)
    p_sweep.add_argument("--seed-radius", type=float, default=2.0)
    p_sweep.add_argument("--min-radius", type=float, default=0.25)
    p_sweep.add_argument("--max-session-calls", type=int, default=1800)
    p_sweep.set_defaults(func=cmd_sweep)

    p_join = sub.add_parser("join", help="zero API calls, safe to rerun anytime")
    p_join.add_argument("--cache", default=DEFAULT_CACHE)
    p_join.add_argument("--out", default="ri_data_statewide")
    p_join.set_defaults(func=cmd_join)

    p_migrate = sub.add_parser("migrate", help="fold an old per-town run's records into the shared cache")
    p_migrate.add_argument("--cache", default=DEFAULT_CACHE)
    p_migrate.add_argument("--scan-dir", required=True,
                            help="an old offmarket-raw/ directory, e.g. ./ri_data_statewide/offmarket-raw")
    p_migrate.set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()