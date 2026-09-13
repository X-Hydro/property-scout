"""
Property Values Database — ingestion orchestrator

Runs one or more state spiders, each writing GeoJSON to --out. Mirrors
AuctionScout's run-scout.py pattern: a REGISTRY dict mapping a short key
to a spider class, selected on the command line.

Each state's constructor needs different arguments (CT and MA need none;
NH needs granit_geojson/town_slug/pid_end/out_dir) -- rather than a
growing if/elif per state (which doesn't actually scale to "any spider,"
it just hardcodes each one), SPIDER_KWARGS below maps each state key to
which of this orchestrator's CLI args its constructor wants, by parameter
name. A spider needing no special args (CT, MA) just doesn't appear in
SPIDER_KWARGS at all. Adding a new spider means adding one line here, not
a new branch.

KNOWN LIMITATION, NH only: --granit-geojson (a pre-downloaded GRANIT
file) only covers ONE town, so running NH against multiple --towns in one
command only works via the live granit_parcel_downloader fetch path (the
default), not with --granit-geojson set. MA no longer has this limitation
-- it was file-based and single-town at first, but was rewritten as a
live query (see ma_spider.py's module docstring), same as CT.

FIXED: NH's constructor now also takes out_dir (see nh_spider.py's module
docstring -- its intermediate files, previously hardcoded to the current
working directory regardless of --out, now write under --out like
everything else this orchestrator produces). Added "out_dir": "out" to
SPIDER_KWARGS["nh"] below so that actually reaches the spider; --out
itself was already being parsed, just never threaded through to NH's
constructor before now.

Usage:
    python run_ingest.py --state ct --towns Bristol "New Haven" --out data/
    python run_ingest.py --state nh --towns Lincoln --pid-end 20000 --out data/
    python run_ingest.py --state ma --towns Andover --out data/
    python run_ingest.py --state ct nh --towns Bristol Lincoln --out data/
    python run_ingest.py --state ma --all-towns --out ma_data
    python run_ingest.py --state nh --towns Lincoln --out nh_data_v2/
    python run_ingest.py --state nj --towns Newark "Jersey City" --out data/
    python run_ingest.py --state md --towns 03 --out data/   # 03 is a JURSCODE, not a county name -- see SPIDER_KWARGS comment below


"""

import argparse
import sys
from pathlib import Path

from spiders.ct.ct_spider import CTSpider
from spiders.nh.nh_spider import NHSpider
from spiders.ma.ma_spider import MASpider
from spiders.md.md_spider import MDSpider
from spiders.nj.nj_spider import NJSpider
from spiders.vt.vt_spider import VTSpider
from spiders.axisgis.axisgis_spider import AxisGISSpider

REGISTRY = {
    "ct": CTSpider,
    "nh": NHSpider,
    "ma": MASpider,
    "md": MDSpider,
    "nj": NJSpider,
    "vt": VTSpider,
}

# Maps each state key to which of this file's CLI arg names (dest, with
# underscores) its spider's __init__ wants, by parameter name. A spider
# needing no special construction args (like CT) just doesn't appear here.
SPIDER_KWARGS = {
    "nh": {
        "granit_geojson": "granit_geojson",
        "town_slug": "town_slug",
        "pid_end": "pid_end",
        "out_dir": "out",
        "value_source": "value_source",
        "min_radius": "min_radius",
        "max_calls": "max_calls",
    },
    # ma, md, nj intentionally absent -- none of their spiders take
    # constructor args (MASpider since its live-query rewrite, see
    # ma_spider.py's docstring; MDSpider/NJSpider from the start, same
    # shape as CTSpider).
}

# MD ONLY, CALLER BEWARE: MDSpider.fetch_town() takes a JURSCODE (e.g.
# "03"), not a county name -- md_spider.py's list_towns() returns real
# JURSCODE values but no live-confirmed code->name table exists yet
# (see md_spider.py's module docstring). So `--towns` for md means
# "pass JURSCODE values", not friendly county names, unlike every other
# state here. Run `python -m spiders.md.md_spider --list-jurisdictions`
# first if you don't already know the code you want.

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
    parser.add_argument("--value-source", choices=["vgsi", "offmarket"], default="vgsi",
                         help="NH only: 'vgsi' (default, per-town VGSI scraping) or 'offmarket' "
                              "(RealtyAPI/Zillow off-market values via a listing-seeded grid, "
                              "split recursively where dense -- see nh_spider.py's module "
                              "docstring). Ignored for every other state.")
    parser.add_argument("--min-radius", type=float, default=0.25,
                         help="NH offmarket path only: recursive splitting floor in miles (default 0.25)")
    parser.add_argument("--max-calls", type=int, default=200,
                         help="NH offmarket path only: PER-TOWN API call budget (default 200) -- "
                              "not a statewide budget; a multi-town run can still exceed your "
                              "RealtyAPI plan's monthly cap if you run too many towns in one month, "
                              "see nh_spider.py's module docstring for the confirmed budget math")
    parser.add_argument("-e", "--stop-on-error", action="store_true",
                         help="stop immediately if any town fails, instead of the default "
                              "behavior of logging the failure to <out>/<state>_failed.txt "
                              "and continuing with the remaining towns")
    args = parser.parse_args()

    overall_summary = {}
    overall_failures = {}
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

        # Run one town at a time, rather than handing the whole list to
        # spider.run() in a single call, specifically so one town's
        # failure can't take down every town after it -- each call is
        # isolated, matching the pattern AuctionScout's per-spider
        # try/except in run-scout.py already uses for the same reason.
        # Writes to <out>/<state>_failed.txt so a long unattended run
        # (e.g. 39 NH towns) leaves a record of exactly what needs a
        # re-run, instead of scrolling terminal output being the only
        # trace of what failed.
        summary = []
        failures = []
        for town in towns:
            try:
                town_summary = spider.run([town], args.out)
                summary.extend(town_summary)
            except Exception as e:
                print(f"  [{state_key}] {town}: FAILED -- {e}")
                failures.append((town, str(e)))
                if args.stop_on_error:
                    raise

        overall_summary[state_key] = summary
        overall_failures[state_key] = failures

        if failures:
            failed_path = Path(args.out) / f"{state_key}_failed.txt"
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            with open(failed_path, "w") as f:
                for town, err in failures:
                    f.write(f"{town}\t{err}\n")
            print(f"[{state_key}] {len(failures)} town(s) failed -- see {failed_path}")

    print("\n" + "=" * 60)
    print("INGESTION SUMMARY")
    for state_key, summary in overall_summary.items():
        for town, count in summary:
            print(f"  {state_key.upper()} {town}: {count} records")
    if any(overall_failures.values()):
        print("-" * 60)
        print("FAILURES")
        for state_key, failures in overall_failures.items():
            for town, err in failures:
                print(f"  {state_key.upper()} {town}: {err}")
    print("=" * 60)

    # Nonzero exit on any failure -- previously always exited 0 even when
    # towns failed internally, which meant a caller checking $? (e.g. a
    # shell loop invoking this once per town) could never tell a failed
    # run apart from a clean one without parsing stdout.
    if any(overall_failures.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()