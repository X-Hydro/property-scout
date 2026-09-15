"""
Join GRANIT parcels + RealtyAPI/Zillow offmarket values — ValueGap (NH path)

This is NH's only value join now (the earlier VGSI/MBLU-based join and
its town-scoped CSV pairing were removed 2026-09). The offmarket source
has NO shared key with GRANIT parcels at all -- no MBLU, no matching
filename convention -- only a raw latitude/longitude per off-market
property. So the join here is spatial (point-in-polygon), not a dict
lookup on a normalized string key.

WHY THE OFFMARKET POINTS CAN'T BE PAIRED PER-TOWN LIKE THE PARCEL FILES:
RealtyAPI's /search/offmarket is ZIP-scoped, and zip boundaries don't
align with town boundaries -- a single zip can span parts of several
towns, and a town can contain several zips. Pairing "town X's parcels"
with "the zip file that happens to share X's name" would silently miss
real matches near any town/zip boundary. Instead, every offmarket point
from every swept zip is loaded into ONE combined spatial index first, and
every parcel (across every town file) is matched against that whole
combined set.

OUTPUT SCHEMA: `total_market_value` is the field name load_property_values.py
reads for assessed_value, confirmed against that script's actual COLUMNS
list.

MATCHING STRATEGY:
  1. Primary: strict point-in-polygon (shapely .within) against the parcel
     geometry, using every offmarket point's (longitude, latitude).
  2. Multiple points landing in the same parcel (real and expected --
     condo/multi-unit buildings, like the 36 Lodge Rd complex confirmed
     earlier in this project, share ONE parcel polygon across many units):
     the point closest to the parcel's centroid is used as the
     representative value, and match_point_count records how many
     candidates existed so a high count is visible/spot-checkable rather
     than silently averaged away. A parcel-level value is inherently a
     compromise for multi-unit buildings -- there's no per-unit geometry
     to do better with here.
  3. NO fallback: if no point strictly falls inside a parcel, that parcel
     gets no value (match=None). A near-miss fallback (nearest point
     within ~40-50m) was tried and removed 2026-09 -- it was silently
     attributing a nearby, unrelated property's value AND address to a
     parcel it wasn't actually inside, which looked like real data under
     an authoritative-looking source tag (NH_GRANIT_OFFMARKET) and
     corrupted gap-analysis comps. See conversation re: Freedom NH's
     "144 West Bay Road" parcel, which is what surfaced this. A NULL
     value is a visible, honest gap; a wrong value silently masquerading
     as a real one is worse.

REQUIRES shapely>=2.0 -- STRtree.query() returns integer indices into the
input geometry list in 2.x, not geometries directly as in 1.x. This script
assumes the 2.x integer-index behavior.

Usage:
    python join_parcels_offmarket.py <parcels_dir> <offmarket_dir> <out_dir>

    parcels_dir:   directory of per-town GRANIT geojson files (output of
                   run_spiders.py's GRANIT-only NH fetch -- geometry, no
                   assessed values)
    offmarket_dir: directory of per-zip JSON files (output of
                   runOffMarketDownloader.sh sweeping /search/offmarket)
    out_dir:       one joined geojson per input town file, same filenames,
                   ready for load_property_values.py
"""

import sys
import json
import glob
import os

from shapely.geometry import shape, Point
from shapely.strtree import STRtree

# Which offmarket field becomes `total_market_value` -- first non-null field
# in this list wins, per match. Default: tax_assessed_value (parity with
# VGSI's real tax-assessment semantics in MA/NJ/VT, so gap analysis compares
# the same KIND of number across every state), falling back to zestimate
# (a market-price estimate, not an assessment) only when no tax assessment
# is present for that property.
#
# Deliberately NOT a CLI flag -- this is a modeling decision (what kind of
# number gap analysis is actually comparing listings against), not a
# per-run convenience setting, so flipping it means editing this constant,
# not something that can be changed accidentally via an unnoticed flag.
# To prefer zestimate instead, reverse the order:
#   VALUE_PRIORITY = ["zestimate", "tax_assessed_value"]
VALUE_PRIORITY = ["tax_assessed_value", "zestimate"]


def _resolve_value(match: dict) -> float | None:
    """First non-null field from VALUE_PRIORITY, in order. Returns None if
    none of them are populated for this match (list_or_last_price is
    intentionally NOT in this chain by default -- it's a list/last-sold
    price, not a value estimate, and mixing it in would silently blend a
    third, differently-biased kind of number into total_market_value; add
    it to VALUE_PRIORITY explicitly if that trade-off is ever wanted)."""
    for field in VALUE_PRIORITY:
        val = match.get(field)
        if val is not None:
            return val
    return None


def load_offmarket_points(offmarket_dir: str, zip_codes: list[str] | None = None):
    """
    Reads offmarket JSON files from offmarket_dir and returns a flat list
    of dicts, one per offmarket property, each holding the parsed shapely
    Point plus the value fields we care about.

    zip_codes: if given, reads ONLY the files matching those zips
    (nh_offmarket_zip<zip>.json), ignoring any other files present in the
    directory. If None (default), reads every *.json file present -- the
    behavior a full statewide run wants, since it's meant to use
    everything swept. The zip_codes filter matters for a scoped run (see
    NHSpider's SCOPED-ZIP MODE): offmarket_dir is REUSED/ACCUMULATED
    across runs (sweeps don't re-fetch or clean up files from earlier
    runs, by design, for caching), so a scoped run globbing the whole
    directory would silently pull in leftover files from an unrelated
    earlier sweep sitting in the same directory -- confirmed to actually
    happen (2026-09-10): a 1-zip scoped Lincoln run loaded 9 zip files'
    worth of points because 8 were left over from an interrupted
    statewide test run. Didn't corrupt that particular result (the other
    zips were too far away to spatially match), but is wrong on principle
    and not something to rely on staying harmless.

    Missing/null fields are kept as None rather than dropping the record --
    a property with no zestimate might still usefully match a parcel via
    tax_assessed_value alone, or vice versa (CONFIRMED from the real
    03251/03784 sweeps: ~88-90% field coverage, not 100%, on both fields
    independently -- they don't always co-occur).
    """
    if zip_codes:
        files = sorted(
            os.path.join(offmarket_dir, f"nh_offmarket_zip{z}.json")
            for z in zip_codes
            if os.path.exists(os.path.join(offmarket_dir, f"nh_offmarket_zip{z}.json"))
        )
        missing = [z for z in zip_codes
                   if not os.path.exists(os.path.join(offmarket_dir, f"nh_offmarket_zip{z}.json"))]
        if missing:
            print(f"  WARNING: requested zip(s) {missing} have no swept file in {offmarket_dir} -- "
                  f"skipped, not an error, but coverage for those zips is absent from this join")
    else:
        files = sorted(glob.glob(os.path.join(offmarket_dir, "*.json")))

    if not files:
        raise SystemExit(f"No usable .json files found under {offmarket_dir}"
                          + (f" for zip(s) {zip_codes}" if zip_codes else ""))

    points = []
    for path in files:
        with open(path) as f:
            payload = json.load(f)
        results = payload.get("offMarketResults", [])
        for rec in results:
            loc = rec.get("location") or {}
            lat = loc.get("latitude")
            lon = loc.get("longitude")
            if lat is None or lon is None:
                continue  # can't spatially join a point with no coordinates

            estimates = rec.get("estimates") or {}
            tax = rec.get("taxAssessment") or {}
            addr = rec.get("address") or {}
            price = rec.get("price") or {}
            lot = rec.get("lotSizeWithUnit") or {}
            acres = lot.get("lotSize") if lot.get("lotSizeUnit") == "acres" else None

            points.append({
                "point": Point(lon, lat),
                "zpid": rec.get("zpid"),
                "address": addr.get("streetAddress"),
                "zestimate": estimates.get("zestimate"),
                "rent_zestimate": estimates.get("rentZestimate"),
                "tax_assessed_value": tax.get("taxAssessedValue"),
                "tax_assessment_year": tax.get("taxAssessmentYear"),
                "list_or_last_price": price.get("value"),
                "acres": acres,
                "source_file": os.path.basename(path),
            })

    print(f"Loaded {len(points)} offmarket point(s) with coordinates across {len(files)} zip file(s)")
    return points


def build_index(points):
    """Builds one STRtree over every offmarket point's geometry, shared
    across all town parcel files -- see module docstring for why this
    can't be scoped per-town/per-zip."""
    geoms = [p["point"] for p in points]
    return STRtree(geoms)


def match_parcel(parcel_geom, tree: STRtree, points: list[dict]):
    """
    Returns (matched_point_dict_or_None, match_method, match_point_count,
    match_distance_deg_or_None).

    match_point_count is the number of points that fell strictly inside
    the parcel (0 for a fallback match, since fallback by definition means
    no strict match existed).
    """
    candidate_idxs = tree.query(parcel_geom)
    inside = []
    for idx in candidate_idxs:
        pt = points[idx]["point"]
        if parcel_geom.contains(pt):
            inside.append(idx)

    if inside:
        if len(inside) == 1:
            return points[inside[0]], "point_in_polygon", 1, 0.0
        centroid = parcel_geom.centroid
        best_idx = min(inside, key=lambda i: points[i]["point"].distance(centroid))
        return points[best_idx], "point_in_polygon", len(inside), points[best_idx]["point"].distance(centroid)

    # No strict containment -- no value assigned for this parcel. (See
    # module docstring: a near-miss fallback used to run here and was
    # removed 2026-09.)
    return None, None, 0, None


def join_town_file(parcels_path: str, tree: STRtree, points: list[dict], out_path: str):
    with open(parcels_path) as f:
        parcels = json.load(f)

    matched_strict = 0
    matched_fallback = 0
    multi_candidate_count = 0
    unmatched = 0
    joined_features = []

    for feature in parcels["features"]:
        props = feature["properties"]
        try:
            geom = shape(feature["geometry"])
        except Exception as e:
            print(f"  WARNING: unparseable geometry for a parcel in {parcels_path} "
                  f"(PID={props.get('PID')}): {e} -- kept with null value fields")
            geom = None

        match, method, n_candidates, distance = (None, None, 0, None)
        if geom is not None:
            match, method, n_candidates, distance = match_parcel(geom, tree, points)

        if match is None:
            unmatched += 1
            props["total_market_value"] = None
            props["offmarket_zpid"] = None
            props["offmarket_address"] = None
            props["tax_assessed_value"] = None
            props["tax_assessment_year"] = None
            props["match_method"] = None
            props["match_point_count"] = 0
            props["offmarket_acres"] = None
        else:
            # See VALUE_PRIORITY at module level -- tax_assessed_value
            # preferred (parity with VGSI's real assessment semantics),
            # falls back to zestimate only when no tax assessment exists
            # for this property. Edit VALUE_PRIORITY to change which field
            # wins; not exposed as a CLI flag on purpose.
            props["total_market_value"] = _resolve_value(match)
            props["offmarket_zpid"] = match["zpid"]
            props["offmarket_acres"] = match.get("acres")
            props["offmarket_address"] = match["address"]
            props["tax_assessed_value"] = match["tax_assessed_value"]
            props["tax_assessment_year"] = match["tax_assessment_year"]
            props["match_method"] = method
            props["match_point_count"] = n_candidates

            if method == "point_in_polygon":
                matched_strict += 1
            else:
                matched_fallback += 1
            if n_candidates > 1:
                multi_candidate_count += 1

        joined_features.append(feature)  # kept either way -- left join, unmatched parcels get null value fields

    out = {"type": "FeatureCollection", "features": joined_features}
    with open(out_path, "w") as f:
        json.dump(out, f)

    total = len(parcels["features"])
    matched = matched_strict + matched_fallback
    match_rate = matched / total if total else 0
    print(f"  {os.path.basename(parcels_path)}: {total} parcels, "
          f"{matched_strict} point-in-polygon, {matched_fallback} nearest-fallback, "
          f"{unmatched} unmatched ({match_rate:.1%} match rate)"
          + (f", {multi_candidate_count} parcel(s) had >1 candidate point (spot check these)"
             if multi_candidate_count else ""))
    if match_rate < 0.5:
        print(f"    WARNING: match rate below 50% for this town -- spot check before trusting "
              f"this output. Common causes: town not covered by the zip-selection threshold "
              f"(few/no active listings there, see select_offmarket_zips.py), or parcels far "
              f"from any off-market sweep coverage.")

    return total, matched_strict, matched_fallback, unmatched


def main():
    if len(sys.argv) != 4:
        print("Usage: python join_parcels_offmarket.py <parcels_dir> <offmarket_dir> <out_dir>")
        sys.exit(1)

    parcels_dir, offmarket_dir, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)

    points = load_offmarket_points(offmarket_dir)
    if not points:
        print("ERROR: no usable offmarket points loaded -- nothing to join against.")
        sys.exit(1)
    tree = build_index(points)

    parcel_files = sorted(glob.glob(os.path.join(parcels_dir, "*.geojson")))
    if not parcel_files:
        print(f"ERROR: no .geojson files found under {parcels_dir}")
        sys.exit(1)

    print(f"\nJoining {len(parcel_files)} town parcel file(s) against {len(points)} offmarket point(s)...\n")

    grand_total = grand_strict = grand_fallback = grand_unmatched = 0
    for parcels_path in parcel_files:
        out_path = os.path.join(out_dir, os.path.basename(parcels_path))
        total, strict, fallback, unmatched = join_town_file(parcels_path, tree, points, out_path)
        grand_total += total
        grand_strict += strict
        grand_fallback += fallback
        grand_unmatched += unmatched

    grand_matched = grand_strict + grand_fallback
    grand_rate = grand_matched / grand_total if grand_total else 0
    print(f"\nDone. {grand_total} parcels across {len(parcel_files)} town file(s) -> {out_dir}")
    print(f"  {grand_strict} point-in-polygon, {grand_fallback} nearest-fallback, "
          f"{grand_unmatched} unmatched ({grand_rate:.1%} overall match rate)")


if __name__ == "__main__":
    main()