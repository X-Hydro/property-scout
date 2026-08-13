"""
Compute gap for a single listing — ValueGap (Phase 2: price analysis)

Finds a listing's neighbors (reusing find_abutters.py's resolution and
neighbor-finding logic -- geometric touching, within 100m + similar size,
or within 250m + same street), then restricts to neighbors that are
genuinely COMPARABLE before using them as a comp. Three conditions, all
required, applied uniformly regardless of which rule found the candidate:
  1. Nearby        -- guaranteed structurally by the finding rules above
  2. Similar size   -- lot_size_similar() against the target's own acreage;
                        geometric touching and the 250m same-street rule
                        don't check size on their own, so this is enforced
                        here too, not assumed from how the candidate was
                        found. Skipped for Land targets -- a land listing's
                        own acreage isn't what matters for comparability,
                        the question is what the neighborhood supports.
  3. Comparable type -- land_use_desc in VALUE_COMP_LAND_USE (Single
                        Family only -- built homes, not land). Deliberately
                        narrower than find_abutters.py's COMP_ELIGIBLE_LAND_USE
                        (which also allows Vacant Land as a candidate worth
                        showing): pooling a $165K vacant lot's price with
                        $1.5M built homes into one median doesn't make
                        sense, even though the lot is a legitimate nearby
                        candidate. A target's value -- whether it's a house
                        being priced, or land being evaluated for
                        development upside -- is measured against real
                        built-home values, never against land prices.
Each comp's assessed value is reported alongside the listing's asking
price, plus a median-based gap.

compute_gap_for_listing() is the reusable core, used by both modes below:
  - Single listing (pass an address): full comp table for ONE listing --
    a *report*, not a verdict, so you can eyeball every candidate (used
    or not, and why) before trusting the gap number.
  - Rank all (pass --rank <output.csv>): runs every eligible listing,
    sorts largest gap to smallest -- the actual leads list. Cross-check
    any promising result with the single-listing mode before acting on it.

Usage:
    python compute_gap.py lincoln_nh.geojson lincoln_joined.geojson lincoln_sfh_land.json "184 Crooked Mountain Rd"
    python compute_gap.py lincoln_nh.geojson lincoln_joined.geojson lincoln_sfh_land.json --rank ranked_gaps.csv
"""

import sys
import csv

from find_abutters import (
    load_json, build_address_index, resolve_listing_to_parcel,
    find_neighbors, find_nearby_by_rules, polygon_area_acres,
    lot_size_similar, COMP_ELIGIBLE_LAND_USE, VALUE_COMP_LAND_USE, ALLOWED_PROPERTY_TYPES,
)


def parse_value(raw) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def build_lookups(joined_features: list[dict]) -> tuple[dict, dict]:
    land_use_by_pid = {}
    value_by_pid = {}
    for f in joined_features:
        pid = str(f["properties"].get("PID", "")).strip()
        land_use_by_pid[pid] = f["properties"].get("land_use_desc")
        value_by_pid[pid] = parse_value(f["properties"].get("total_market_value"))
    return land_use_by_pid, value_by_pid


def compute_gap_for_listing(
    listing: dict,
    raw_features: list[dict],
    address_index: dict,
    land_use_by_pid: dict,
    value_by_pid: dict,
) -> dict | None:
    """
    Core gap computation for one listing, shared by the single-listing CLI
    below and rank_gaps.py's batch mode. Returns a dict with the full
    candidate table plus the computed gap, or a dict with 'error' set (and
    no gap) if the listing couldn't be resolved or had no usable comps --
    callers decide how to report that, this function doesn't print anything.
    """
    parcel, method = resolve_listing_to_parcel(listing, address_index, raw_features)
    if parcel is None:
        return {"listing": listing, "error": "unresolved", "resolve_method": method}

    target_acres = polygon_area_acres(parcel["geometry"])
    target_is_land = listing.get("propertyType") == "Land"

    geometric = find_neighbors(raw_features, parcel)
    nearby = find_nearby_by_rules(raw_features, parcel)

    by_pid: dict[str, tuple[dict, set]] = {}
    for n in geometric:
        pid = str(n["properties"].get("PID", "")).strip()
        by_pid.setdefault(pid, (n, set()))[1].add("geometric")
    for pid, (n, reason) in nearby.items():
        by_pid.setdefault(pid, (n, set()))[1].add(reason)

    candidates = []
    comps = []
    for pid, (n, reasons) in sorted(by_pid.items()):
        addr = n["properties"].get("StreetAddress") or ""
        land_use = land_use_by_pid.get(pid)
        value = value_by_pid.get(pid)
        acres = round(polygon_area_acres(n["geometry"]), 2)
        # VALUE_COMP_LAND_USE (Single Family only) is deliberately narrower
        # than COMP_ELIGIBLE_LAND_USE -- a vacant lot is a legitimate
        # candidate to show, but pooling its price with built homes into
        # one median doesn't make sense (a $165K lot next to $1.5M houses
        # would drag or skew the median for the wrong reason). The target's
        # value -- house or land -- is measured against real built homes.
        type_ok = land_use in VALUE_COMP_LAND_USE
        size_ok = True if target_is_land else lot_size_similar(target_acres, acres)
        used = type_ok and size_ok and value is not None

        candidates.append({
            "pid": pid, "address": addr, "land_use_desc": land_use,
            "acres": acres, "size_ok": size_ok, "value": value,
            "found_via": "+".join(sorted(reasons)), "used": used,
        })
        if used:
            comps.append(value)

    if not comps:
        return {
            "listing": listing, "error": "no_comps", "resolve_method": method,
            "target_acres": target_acres, "candidates": candidates,
        }

    comps.sort()
    n_comps = len(comps)
    median = comps[n_comps // 2] if n_comps % 2 else (comps[n_comps // 2 - 1] + comps[n_comps // 2]) / 2
    price = listing.get("price")
    gap = median - price if price else None
    pct = (gap / price) * 100 if (gap is not None and price) else None

    return {
        "listing": listing, "error": None, "resolve_method": method,
        "target_acres": target_acres, "target_is_land": target_is_land,
        "candidates": candidates, "comps": comps, "comp_count": n_comps,
        "comp_median": median, "comp_min": min(comps), "comp_max": max(comps),
        "price": price, "gap": gap, "gap_pct": pct,
    }


def print_report(result: dict):
    """Full comp-table report for one listing -- the CLI mode below."""
    listing = result["listing"]
    print(f"Listing: {listing.get('addressLine1')} | ${listing.get('price'):,} | "
          f"{listing.get('propertyType')} | {listing.get('squareFootage')} sqft | "
          f"resolved via {result.get('resolve_method')}")
    print()

    if result["error"] == "unresolved":
        print("Could not resolve this listing to a parcel (neither point-in-polygon nor address match worked).")
        return

    print(f"Target lot size: {round(result['target_acres'], 2)} acres")
    print()
    print(f"{'PID':<16} {'Address':<28} {'LandUse':<16} {'Acres':>6} {'SizeOK':>7} {'AssessedValue':>14}  FoundVia")
    print("-" * 110)
    for c in result["candidates"]:
        value_str = f"${c['value']:,.0f}" if c["value"] is not None else "no VGSI match"
        flag = "  <- used" if c["used"] else ""
        print(f"{c['pid']:<16} {c['address']:<28} {str(c['land_use_desc']):<16} {c['acres']:>6} "
              f"{str(c['size_ok']):>7} {value_str:>14}  {c['found_via']}{flag}")

    comps_used = sum(1 for c in result["candidates"] if c["used"])
    print()
    print(f"Comparable comps used (matching size + type, with a usable assessed value): "
          f"{comps_used} of {len(result['candidates'])} total neighbors found")
    if result.get("target_is_land"):
        print("Note: size check skipped for this comp set -- target is Land, "
              "so proximity + type mattered more than lot-size symmetry.")

    if result["error"] == "no_comps":
        print("No usable comps -- can't compute a gap. Likely cause: missing VGSI matches "
              "for this cluster (check the addresses above marked 'no VGSI match').")
        return

    print(f"Comp assessed values: min ${result['comp_min']:,.0f}, median ${result['comp_median']:,.0f}, "
          f"max ${result['comp_max']:,.0f}")
    print()
    print(f"Listing price: ${result['price']:,.0f}")
    print(f"Gap vs. comp median: ${result['gap']:,.0f} ({result['gap_pct']:+.1f}%)")
    print()
    print("Reminder: this is a lead, not a verdict. Eyeball the comp table above -- "
          "a small comp count, a wide value spread, or a comp that doesn't actually "
          "look comparable (check land_use_desc and acres) should make you trust the "
          "gap number less, not more.")


def run_rank_mode(raw_features, joined_features, listings, out_path):
    """Compute gaps for every eligible listing. Land and Single Family are
    ranked SEPARATELY (each sorted largest gap to smallest) rather than
    mixed into one combined ranking -- a land gap and a house gap aren't
    really the same kind of number, so pooling them into one sort doesn't
    give a meaningful ordering."""
    land_use_by_pid, value_by_pid = build_lookups(joined_features)
    address_index = build_address_index(raw_features)
    eligible = [l for l in listings if l.get("propertyType") in ALLOWED_PROPERTY_TYPES]

    ranked, unresolved, no_comps = [], [], []
    for listing in eligible:
        result = compute_gap_for_listing(listing, raw_features, address_index, land_use_by_pid, value_by_pid)
        if result["error"] == "unresolved":
            unresolved.append(listing.get("addressLine1"))
        elif result["error"] == "no_comps":
            no_comps.append(listing.get("addressLine1"))
        else:
            ranked.append({
                "address": listing.get("addressLine1"), "property_type": listing.get("propertyType"),
                "price": result["price"], "comp_median": result["comp_median"],
                "comp_count": result["comp_count"], "comp_min": result["comp_min"],
                "comp_max": result["comp_max"], "gap": result["gap"], "gap_pct": result["gap_pct"],
            })

    land_ranked = sorted((r for r in ranked if r["property_type"] == "Land"), key=lambda r: r["gap"], reverse=True)
    sfh_ranked = sorted((r for r in ranked if r["property_type"] == "Single Family"), key=lambda r: r["gap"], reverse=True)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "address", "property_type", "price", "comp_median", "comp_count",
            "comp_min", "comp_max", "gap", "gap_pct",
        ])
        writer.writeheader()
        writer.writerows(land_ranked)   # Land block first, each independently sorted
        writer.writerows(sfh_ranked)    # then Single Family block

    print(f"Eligible listings: {len(eligible)}")
    print(f"Ranked (usable gap computed): {len(ranked)} ({len(land_ranked)} Land, {len(sfh_ranked)} Single Family)")
    print(f"Unresolved (couldn't locate parcel): {len(unresolved)}")
    for a in unresolved:
        print(f"  {a}")
    print(f"No usable comps: {len(no_comps)}")
    for a in no_comps:
        print(f"  {a}")
    print()
    print(f"Wrote {out_path} -- Land and Single Family each sorted largest gap to smallest, Land block first")

    def print_section(title, rows):
        print()
        print(f"=== {title} ({len(rows)}) ===")
        print(f"{'Address':<28} {'Price':>12} {'CompMedian':>12} {'Comps':>6} {'Gap':>12} {'Gap%':>8}")
        print("-" * 90)
        for r in rows[:15]:
            print(f"{r['address']:<28} ${r['price']:>10,.0f} "
                  f"${r['comp_median']:>10,.0f} {r['comp_count']:>6} "
                  f"${r['gap']:>10,.0f} {r['gap_pct']:>7.1f}%")
        if len(rows) > 15:
            print(f"... and {len(rows) - 15} more in {out_path}")

    print_section("Land -- largest gap first", land_ranked)
    print_section("Single Family -- largest gap first", sfh_ranked)

    print()
    print("Reminder: this is a lead list, not a verdict. A big gap can also mean a small "
          "comp count, a confound we haven't filtered, or bad data for that cluster -- "
          "check comp_count and cross-reference any promising result with the single-listing "
          "report (rerun with a specific address instead of --rank) before trusting it.")


def main():
    if len(sys.argv) < 5:
        print('Usage:')
        print('  Single listing:  python compute_gap.py <raw.geojson> <joined.geojson> <listings.json> "<addressLine1>"')
        print('  Rank all:        python compute_gap.py <raw.geojson> <joined.geojson> <listings.json> --rank <output.csv>')
        sys.exit(1)

    raw_path, joined_path, listings_path, mode = sys.argv[1:5]
    raw_features = load_json(raw_path)["features"]
    listings = load_json(listings_path)
    joined_features = load_json(joined_path)["features"]

    if mode == "--rank":
        if len(sys.argv) != 6:
            print('Usage: python compute_gap.py <raw.geojson> <joined.geojson> <listings.json> --rank <output.csv>')
            sys.exit(1)
        run_rank_mode(raw_features, joined_features, listings, sys.argv[5])
        return

    target_address = mode
    land_use_by_pid, value_by_pid = build_lookups(joined_features)

    listing = next((l for l in listings if l.get("addressLine1", "").strip().lower() == target_address.strip().lower()), None)
    if listing is None:
        print(f"No listing found with addressLine1 == {target_address!r}")
        sys.exit(1)

    address_index = build_address_index(raw_features)
    result = compute_gap_for_listing(listing, raw_features, address_index, land_use_by_pid, value_by_pid)
    print_report(result)


if __name__ == "__main__":
    main()