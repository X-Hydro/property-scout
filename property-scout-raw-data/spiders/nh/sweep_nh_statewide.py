"""
Statewide NH offmarket value sweep, resumable across multiple monthly runs.

WHY THIS EXISTS: CONFIRMED (2026-09) 240 of NH's 259 named towns have
active listings -- scoping the sweep to "towns with listings" only saves
~19 towns, not a meaningful reduction. Combined with a real measured
per-town cost of ~35 calls (Lincoln) and 259 total towns, a full
statewide pass is roughly 259 * 35 ~= 9,065 calls -- far over the
confirmed 2,000/month RealtyAPI budget (Lincoln may not be
representative; no dense-city data point exists yet, so this could be an
underestimate). There's no way to fit this in one pass -- this script
spreads it across however many monthly runs it actually takes.

CHECKPOINT FILE (default nh_statewide_progress.json): a simple JSON list
of town names already successfully swept. Saved after EVERY town, not
just at the end of a session -- a crash or interruption mid-run doesn't
lose progress on towns already completed. Delete a town's name from this
file (or the whole file) to force it to be re-swept.

BUDGET SAFETY: stops starting a NEW town once (calls used so far this
session) + (--max-calls, the per-town ceiling) would exceed
--monthly-budget. This guarantees the session can never exceed budget
even in the worst case where the next town hits its full per-town cap --
which means a run may stop meaningfully short of the full budget if the
next town's worst case wouldn't fit. That's intentional conservatism
given how tight the real cap is; raise --max-calls or --monthly-budget
if this leaves too much budget unused in practice.

Town list source: a New England town boundaries GeoJSON (e.g.
newengland_town_boundaries.json), filtered to NH, excluding unnamed
features.

Usage:
    python sweep_nh_statewide.py newengland_town_boundaries.json \
        --out ./nh_data_statewide --monthly-budget 1800

    # Following month, same command -- picks up automatically where the
    # checkpoint left off:
    python sweep_nh_statewide.py newengland_town_boundaries.json \
        --out ./nh_data_statewide --monthly-budget 1800
"""

import sys
import os
import json
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent  # spiders/nh/ -> spiders/ -> project root
sys.path.insert(0, str(PROJECT_ROOT))
from spiders.nh.nh_spider import NHSpider


def load_nh_town_list(boundaries_path: str) -> list[str]:
    with open(boundaries_path) as f:
        data = json.load(f)
    return sorted(
        f["properties"]["name"] for f in data["features"]
        if f["properties"].get("a1_admin_code") == "NH" and f["properties"].get("name")
    )


def load_checkpoint(checkpoint_path: str) -> set[str]:
    if not os.path.exists(checkpoint_path):
        return set()
    with open(checkpoint_path) as f:
        return set(json.load(f))


def save_checkpoint(checkpoint_path: str, completed: set[str]):
    with open(checkpoint_path, "w") as f:
        json.dump(sorted(completed), f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("boundaries_geojson", help="New England town boundaries GeoJSON")
    parser.add_argument("--out", default="nh_data_statewide")
    parser.add_argument("--checkpoint", default="nh_statewide_progress.json")
    parser.add_argument("--monthly-budget", type=int, default=1800,
                         help="stop starting new towns once this session's calls + the next "
                              "town's worst case (--max-calls) would exceed this (default 1800, "
                              "leaving headroom under a 2000/month cap for other RealtyAPI usage "
                              "elsewhere in the project)")
    parser.add_argument("--seed-radius", type=float, default=2.0)
    parser.add_argument("--min-radius", type=float, default=0.25)
    parser.add_argument("--max-calls", type=int, default=200,
                         help="PER-TOWN ceiling, also used as the worst-case reserve for the "
                              "budget-safety check above")
    args = parser.parse_args()

    all_towns = load_nh_town_list(args.boundaries_geojson)
    completed = load_checkpoint(args.checkpoint)
    remaining = [t for t in all_towns if t not in completed]

    print(f"{len(all_towns)} total NH town(s), {len(completed)} already completed, "
          f"{len(remaining)} remaining\n")

    if not remaining:
        print("Nothing left to do -- statewide sweep is already complete per the checkpoint.")
        return

    spider = NHSpider(
        out_dir=args.out,
        value_source="offmarket",
        seed_radius=args.seed_radius,
        min_radius=args.min_radius,
        max_calls=args.max_calls,
    )

    session_completed = []
    session_failed = []

    for town in remaining:
        if spider.total_api_calls + args.max_calls > args.monthly_budget:
            print(f"\nSTOPPING: {spider.total_api_calls} call(s) used this session, next town's "
                  f"worst case ({args.max_calls}) would exceed --monthly-budget "
                  f"{args.monthly_budget}. {len(remaining) - len(session_completed)} town(s) "
                  f"remain for next month's run.")
            break

        print(f"\n=== {town} ({len(completed) + len(session_completed) + 1}/{len(all_towns)} "
              f"statewide) ===")
        try:
            spider.run([town], args.out)
            session_completed.append(town)
            completed.add(town)
            save_checkpoint(args.checkpoint, completed)
        except Exception as e:
            print(f"  FAILED: {e}")
            session_failed.append((town, str(e)))

    print(f"\n{'=' * 60}")
    print(f"SESSION DONE: {len(session_completed)} town(s) completed, {len(session_failed)} "
          f"failed, {spider.total_api_calls} API call(s) used this session")
    print(f"Statewide progress: {len(completed)}/{len(all_towns)} town(s) complete")
    if session_failed:
        print("Failed towns (NOT marked complete -- will retry next run):")
        for town, err in session_failed:
            print(f"  {town}: {err}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()