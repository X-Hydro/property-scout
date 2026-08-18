"""
Property Values Database — ingestion orchestrator

Runs one or more state spiders, each writing GeoJSON to --out. Mirrors
AuctionScout's run-scout.py pattern: a REGISTRY dict mapping a short key
to a spider class, selected on the command line.

Each state's constructor needs different arguments (CT and MA need none;
NH needs granit_geojson/town_slug/pid_end) -- rather than a growing
if/elif per state (which doesn't actually scale to "any spider," it just
hardcodes each one), SPIDER_KWARGS below maps each state key to which of
this orchestrator's CLI args its constructor wants, by parameter name. A
spider needing no special args (CT, MA) just doesn't appear in
SPIDER_KWARGS at all. Adding a new spider means adding one line here, not
a new branch.

KNOWN LIMITATION, NH only: --granit-geojson (a pre-downloaded GRANIT
file) only covers ONE town, so running NH against multiple --towns in one
command only works via the live granit_parcel_downloader fetch path (the
default), not with --granit-geojson set. MA no longer has this limitation
-- it was file-based and single-town at first, but was rewritten as a
live query (see ma_spider.py's module docstring), same as CT.

Usage:
    python run_ingest.py --state ct --towns Bristol "New Haven" --out data/
    python run_ingest.py --state nh --towns Lincoln --pid-end 20000 --out data/
    python run_ingest.py --state ma --towns Andover --out data/
    python run_ingest.py --state ct nh --towns Bristol Lincoln --out data/
    python run_ingest.py --state ma --all-towns --out ma_data
"""

import argparse
import sys

from spiders.ct.ct_spider import CTSpider
from spiders.nh.nh_spider import NHSpider
from spiders.ma.ma_spider import MASpider
from spiders.axisgis.axisgis_spider import AxisGISSpider

REGISTRY = {
    "ct": CTSpider,
    "nh": NHSpider,
    "ma": MASpider,
}

# Maps each state key to which of this file's CLI arg names (dest, with
# underscores) its spider's __init__ wants, by parameter name. A spider
# needing no special construction args (like CT) just doesn't appear here.
SPIDER_KWARGS = {
    "nh": {
        "granit_geojson": "granit_geojson",
        "town_slug": "town_slug",
        "pid_end": "pid_end",
    },
    # ma intentionally absent -- MASpider takes no constructor args since
    # its rewrite as a live query (was file-based, needed geojson_path;
    # removed when that changed, see ma_spider.py's module docstring).
}

# Same pattern as AuctionScout's KNOWN_UNAVAILABLE dict: imported and
# visible in the codebase, but deliberately NOT in REGISTRY, so nothing
# can select it and make a live request until it's been explicitly
# activated (see spiders/axisgis/axisgis_spider.py's module docstring
# for why -- robots.txt currently disallows automated access).
KNOWN_UNAVAILABLE = {
    "axisgis": AxisGISSpider,
}


def _build_spider(state_key: str, args: argparse.Namespace):
    spider_cls = REGISTRY[state_key]
    kwarg_map = SPIDER_KWARGS.get(state_key, {})
    kwargs = {ctor_param: getattr(args, arg_name) for ctor_param, arg_name in kwarg_map.items()}
    return spider_cls(**kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", nargs="+", required=True,
                         help="one or more state keys to run")
    towns_group = parser.add_mutually_exclusive_group(required=True)
    towns_group.add_argument("--towns", nargs="+",
                              help="town/municipality names, applied to every selected state")
    towns_group.add_argument("--all-towns", action="store_true",
                              help="run every municipality the spider's own source knows about "
                                   "(requires the spider to implement list_towns() -- CT and MA "
                                   "do; NH does not yet)")
    parser.add_argument("--out", default="data")
    parser.add_argument("--pid-end", type=int, default=20000, help="NH only")
    parser.add_argument("--town-slug", help="NH only, single-town VGSI slug override")
    parser.add_argument("--granit-geojson", help="NH only, single-town pre-downloaded geojson")
    args = parser.parse_args()

    overall_summary = {}
    for state_key in args.state:
        if state_key in KNOWN_UNAVAILABLE:
            print(f"ERROR: '{state_key}' is a known stub, not yet permitted to run live. "
                  f"See spiders/{state_key}/{state_key}_spider.py's module docstring.")
            sys.exit(1)
        if state_key not in REGISTRY:
            print(f"ERROR: unknown state key '{state_key}'. Available: {sorted(REGISTRY)}")
            sys.exit(1)

        spider = _build_spider(state_key, args)

        if args.all_towns:
            if not hasattr(spider, "list_towns"):
                print(f"ERROR: '{state_key}' spider doesn't implement list_towns() yet -- "
                      f"pass --towns explicitly instead.")
                sys.exit(1)
            print(f"[{state_key}] discovering full town list...")
            towns = spider.list_towns()
            print(f"[{state_key}] {len(towns)} municipalities found, running all of them")
        else:
            towns = args.towns

        summary = spider.run(towns, args.out)
        overall_summary[state_key] = summary

    print("\n" + "=" * 60)
    print("INGESTION SUMMARY")
    for state_key, summary in overall_summary.items():
        for town, count in summary:
            print(f"  {state_key.upper()} {town}: {count} records")
    print("=" * 60)


if __name__ == "__main__":
    main()