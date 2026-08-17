"""
Property Values Database — ingestion orchestrator

Runs one or more state spiders, each writing GeoJSON to --out. Mirrors
AuctionScout's run-scout.py pattern: a REGISTRY dict mapping a short key
to a spider class, selected on the command line.

Each state's CLI args differ too much to unify cleanly (CT just needs
town names; NH needs a pid_end, an optional town-slug override, and an
optional pre-downloaded GRANIT geojson) -- so this orchestrator handles
the common part (which spiders to run, shared --out) and defers to each
spider's own fetch_town() for the state-specific mechanics. For NH
specifically, since --granit-geojson only applies to one town at a time,
running NH through this orchestrator alongside other states currently
only supports the subprocess-download path (see nh_spider.py) -- run
nh_spider.py directly with --granit-geojson for a single already-
downloaded town.

Usage:
    python run_ingest.py --state ct --towns Bristol "New Haven" --out data/
    python run_ingest.py --state nh --towns Lincoln --pid-end 20000 --out data/
    python run_ingest.py --state ct nh --towns Bristol Lincoln --out data/
"""

import argparse
import sys

from spiders.ct_spider import CTSpider
from spiders.nh_spider import NHSpider

REGISTRY = {
    "ct": CTSpider,
    "nh": NHSpider,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", nargs="+", required=True, choices=sorted(REGISTRY),
                         help="one or more state keys to run")
    parser.add_argument("--towns", nargs="+", required=True,
                         help="town/municipality names, applied to every selected state")
    parser.add_argument("--out", default="data")
    parser.add_argument("--pid-end", type=int, default=20000, help="NH only")
    parser.add_argument("--town-slug", help="NH only, single-town VGSI slug override")
    parser.add_argument("--granit-geojson", help="NH only, single-town pre-downloaded geojson")
    args = parser.parse_args()

    overall_summary = {}
    for state_key in args.state:
        spider_cls = REGISTRY[state_key]
        if state_key == "nh":
            spider = spider_cls(
                granit_geojson=args.granit_geojson,
                town_slug=args.town_slug,
                pid_end=args.pid_end,
            )
        else:
            spider = spider_cls()

        summary = spider.run(args.towns, args.out)
        overall_summary[state_key] = summary

    print("\n" + "=" * 60)
    print("INGESTION SUMMARY")
    for state_key, summary in overall_summary.items():
        for town, count in summary:
            print(f"  {state_key.upper()} {town}: {count} records")
    print("=" * 60)


if __name__ == "__main__":
    main()