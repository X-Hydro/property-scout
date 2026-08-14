"""
Compute gap for listings — ValueGap (Phase 2: price analysis + reports)

Reads directly from find_abutters.py's *_combined.geojson output -- NOT
from raw parcels + a fresh re-resolution. find_abutters.py already did the
expensive work (resolving each listing to a parcel, finding geometric +
distance-based neighbors, filtering by size/type) and saved every listing's
candidate set as listing_point / target_parcel / neighbor_parcel features,
grouped by listing_id. This script's only job is the final gap math on top
of that -- no re-scanning thousands of raw parcels, no risk of drifting out
of sync with what find_abutters.py actually found.

One real consequence of this: listings find_abutters.py couldn't resolve
to a parcel at all (e.g. 32 Alpine Dr, whose lot wasn't yet in GRANIT) have
no listing_point written to the combined file, so they're simply absent
here too -- this script can't distinguish "unresolved" from "just not in
this town's listings," since find_abutters.py already reported that
separately in its own run. Check find_abutters.py's own console output for
that.

For each listing, restricts neighbor candidates to genuinely COMPARABLE
ones before using them as a value comp -- three conditions, all required:
  1. Nearby        -- guaranteed structurally by find_abutters.py's rules
  2. Similar size   -- lot_size_similar() against the target's own acreage
                        (skipped for Land targets -- a land listing's own
                        acreage isn't what matters, the question is what
                        the neighborhood supports)
  3. Comparable type -- land_use_desc in VALUE_COMP_LAND_USE (Single
                        Family only -- built homes, not land). Narrower
                        than find_abutters.py's COMP_ELIGIBLE_LAND_USE
                        (which also allows Vacant Land as a candidate
                        worth showing): pooling a $165K vacant lot with
                        $1.5M built homes into one median doesn't make
                        sense, even though the lot is a legitimate
                        neighbor. A target's value -- house or land -- is
                        measured against real built-home values, never
                        against land prices.

Three modes:
  - Single listing (pass an address): full comp table for ONE listing --
    a *report*, not a verdict, so you can eyeball every candidate (used
    or not, and why) before trusting the gap number.
  - Rank all (pass --rank <output.csv>): every listing in the combined
    file, sorted largest gap to smallest per group (Land, Single Family
    ranked separately -- a land gap and a house gap aren't the same kind
    of number). Add --reports <dir> to also write JSON/HTML/KML detail
    files (see below) for every ranked listing, not just print the table.
  - --reports <dir> (with either mode above): for each listing, writes:
      <slug>.json  Full structured detail -- target info + every
                   candidate (used or not, with reasons) + the gap.
      <slug>.html  Human-readable report with an embedded interactive
                   Leaflet map (free, no API key) showing the target
                   parcel, every candidate polygon (color-coded used/
                   excluded), and hooklines to used comps -- click any
                   shape for details, same visual as the QGIS view.
      <slug>.kml   Google My Maps import file (mymaps.google.com ->
                   Import) with the same placemarks/hooklines.

Usage:
    python compute_gap.py lincoln_abutters_combined.geojson "184 Crooked Mountain Rd"
    python compute_gap.py lincoln_abutters_combined.geojson --rank ranked_gaps.csv
    python compute_gap.py lincoln_abutters_combined.geojson --rank ranked_gaps.csv --reports reports/
"""

import sys
import os
import csv
import json
import argparse
import re
from collections import defaultdict

ALLOWED_PROPERTY_TYPES = {"Single Family", "Land"}
VALUE_COMP_LAND_USE = {"Single Family"}


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


def lot_size_similar(acres_a: float, acres_b: float, ratio_tolerance: float = 2.5) -> bool:
    if not acres_a or not acres_b or acres_a <= 0 or acres_b <= 0:
        return False
    lo, hi = sorted([acres_a, acres_b])
    return (hi / lo) <= ratio_tolerance


def slugify(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (address or "unknown").lower()).strip("-")


def group_by_listing(combined_features: list[dict]) -> dict[str, dict]:
    """
    Group find_abutters.py's combined.geojson features by listing_id.
    Returns {listing_id: {"listing_point": feature, "target_parcel": feature,
    "neighbors": [feature, ...]}}. Lines are ignored here -- hooklines are
    regenerated fresh for reports rather than reused, since they're purely
    derived from listing_point + used neighbors anyway.
    """
    groups: dict[str, dict] = defaultdict(lambda: {"listing_point": None, "target_parcel": None, "neighbors": []})
    for f in combined_features:
        p = f["properties"]
        lid = p.get("listing_id")
        if lid is None:
            continue
        ft = p.get("feature_type")
        if ft == "listing_point":
            groups[lid]["listing_point"] = f
        elif ft == "target_parcel":
            groups[lid]["target_parcel"] = f
        elif ft == "neighbor_parcel":
            groups[lid]["neighbors"].append(f)
    return groups


def compute_gap_for_group(group: dict) -> dict | None:
    """
    Core gap computation for one listing's pre-grouped combined.geojson
    features. Returns a result dict (matching the shape the rest of this
    file expects), or None if the group has no listing_point at all
    (shouldn't happen if group_by_listing was used correctly).
    """
    lp_feature = group["listing_point"]
    if lp_feature is None:
        return None
    lp = lp_feature["properties"]

    target_feature = group["target_parcel"]
    target_acres = target_feature["properties"].get("lot_acres") if target_feature else None
    target_assessed_value = target_feature["properties"].get("total_market_value") if target_feature else None
    try:
        target_assessed_value = float(target_assessed_value) if target_assessed_value not in (None, "") else None
    except (ValueError, TypeError):
        target_assessed_value = None
    target_geometry = target_feature["geometry"] if target_feature else None
    target_is_land = lp.get("listing_type") == "Land"

    listing = {
        "addressLine1": lp.get("listing_address"),
        "propertyType": lp.get("listing_type"),
        "price": lp.get("listing_price"),
        "latitude": lp_feature["geometry"]["coordinates"][1] if lp_feature["geometry"] else None,
        "longitude": lp_feature["geometry"]["coordinates"][0] if lp_feature["geometry"] else None,
        "squareFootage": lp.get("listing_sqft"),
        "yearBuilt": lp.get("listing_year_built"),
        "id": lp.get("listing_id"),
    }

    candidates = []
    comps = []
    for n in group["neighbors"]:
        np_ = n["properties"]
        pid = np_.get("pid")
        addr = np_.get("address") or ""
        land_use = np_.get("land_use_desc")
        value = np_.get("total_market_value")
        acres = np_.get("lot_acres")
        try:
            value = float(value) if value not in (None, "") else None
        except (ValueError, TypeError):
            value = None

        type_ok = land_use in VALUE_COMP_LAND_USE
        size_ok = True if target_is_land else lot_size_similar(target_acres, acres)
        used = type_ok and size_ok and value is not None

        geom = n["geometry"]
        # centroid via simple averaging of the first ring -- good enough
        # for a map marker/popup anchor, doesn't need to be exact
        coords = geom["coordinates"][0][0] if geom["type"] == "MultiPolygon" else geom["coordinates"][0]
        lon = sum(pt[0] for pt in coords) / len(coords)
        lat = sum(pt[1] for pt in coords) / len(coords)

        candidates.append({
            "pid": pid, "address": addr, "land_use_desc": land_use,
            "acres": acres, "size_ok": size_ok, "value": value,
            "found_via": np_.get("found_via"), "used": used,
            "lat": lat, "lon": lon, "geometry": geom,
        })
        if used:
            comps.append(value)

    base = {
        "listing": listing, "target_acres": target_acres, "target_is_land": target_is_land,
        "target_assessed_value": target_assessed_value, "target_geometry": target_geometry,
        "candidates": candidates,
    }

    if not comps:
        base["error"] = "no_comps"
        return base

    comps.sort()
    n_comps = len(comps)
    median = comps[n_comps // 2] if n_comps % 2 else (comps[n_comps // 2 - 1] + comps[n_comps // 2]) / 2
    price = listing.get("price")
    gap = median - price if price else None
    pct = (gap / price) * 100 if (gap is not None and price) else None

    base.update({
        "error": None, "comps": comps, "comp_count": n_comps,
        "comp_median": median, "comp_min": min(comps), "comp_max": max(comps),
        "price": price, "gap": gap, "gap_pct": pct,
    })
    return base


# ---------------------------------------------------------------------------
# Single-listing report (console)
# ---------------------------------------------------------------------------

def print_report(result: dict):
    listing = result["listing"]
    print(f"Listing: {listing.get('addressLine1')} | ${listing.get('price'):,} | "
          f"{listing.get('propertyType')} | {listing.get('squareFootage')} sqft | "
          f"built {listing.get('yearBuilt', 'unknown')}")
    tav = result.get("target_assessed_value")
    print(f"Target's own assessed value: {'$' + format(tav, ',.0f') if tav else 'no VGSI match'}")
    print()

    ta = result.get("target_acres")
    print(f"Target lot size: {round(ta, 2) if ta else '?'} acres")
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
    print(f"Comparable comps used: {comps_used} of {len(result['candidates'])} total neighbors found")
    if result.get("target_is_land"):
        print("Note: size check skipped for this comp set -- target is Land.")

    if result["error"] == "no_comps":
        print("No usable comps -- can't compute a gap.")
        return

    print(f"Comp assessed values: min ${result['comp_min']:,.0f}, median ${result['comp_median']:,.0f}, "
          f"max ${result['comp_max']:,.0f}")
    print()
    print(f"Listing price: ${result['price']:,.0f}")
    print(f"Gap vs. comp median: ${result['gap']:,.0f} ({result['gap_pct']:+.1f}%)")
    print()
    print("Reminder: this is a lead, not a verdict.")


# ---------------------------------------------------------------------------
# Report file generation: JSON, HTML (with embedded map), KML
# ---------------------------------------------------------------------------

def write_json_report(result: dict, path: str):
    listing = result["listing"]
    payload = {
        "target": {
            "address": listing.get("addressLine1"), "property_type": listing.get("propertyType"),
            "price": listing.get("price"), "square_footage": listing.get("squareFootage"),
            "year_built": listing.get("yearBuilt"), "latitude": listing.get("latitude"),
            "longitude": listing.get("longitude"), "target_acres": result.get("target_acres"),
            "target_assessed_value": result.get("target_assessed_value"),
        },
        "error": result.get("error"),
        "candidates": [{k: v for k, v in c.items() if k != "geometry"} | {"geometry": c["geometry"]} for c in result.get("candidates", [])],
        "gap_summary": None,
    }
    if result.get("error") is None:
        payload["gap_summary"] = {
            "comp_count": result["comp_count"], "comp_median": result["comp_median"],
            "comp_min": result["comp_min"], "comp_max": result["comp_max"],
            "gap": result["gap"], "gap_pct": result["gap_pct"],
        }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def build_map_geojson(result: dict) -> dict:
    listing = result["listing"]
    lat, lon = listing.get("latitude"), listing.get("longitude")
    features = []

    if lat is not None and lon is not None:
        features.append({
            "type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"kind": "listing", "popup": f"<b>{listing.get('addressLine1')}</b><br>"
                           f"${listing.get('price', 0):,.0f} | {listing.get('propertyType')}"},
        })

    target_geom = result.get("target_geometry")
    if target_geom:
        tav = result.get("target_assessed_value")
        tav_str = f"${tav:,.0f}" if tav else "no VGSI match"
        features.append({
            "type": "Feature", "geometry": target_geom,
            "properties": {"kind": "target", "popup": f"<b>Target parcel</b><br>Assessed: {tav_str}"},
        })

    for c in result.get("candidates", []):
        if not c.get("geometry"):
            continue
        value_str = f"${c['value']:,.0f}" if c["value"] is not None else "no VGSI match"
        features.append({
            "type": "Feature", "geometry": c["geometry"],
            "properties": {
                "kind": "used" if c["used"] else "excluded",
                "popup": f"<b>{c['address']}</b><br>{c['land_use_desc'] or '?'} | {c['acres']} ac<br>"
                         f"{value_str}<br>{'USED in comp' if c['used'] else 'shown for context, not used'}",
            },
        })
        if c["used"] and lat is not None:
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[lon, lat], [c["lon"], c["lat"]]]},
                "properties": {"kind": "hookline", "popup": ""},
            })

    return {"type": "FeatureCollection", "features": features}


def build_map_html(map_geojson: dict) -> str:
    geojson_str = json.dumps(map_geojson)
    return f"""
<div id="map" style="height: 500px; margin-bottom: 1.5em; border-radius: 8px;"></div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const map = L.map('map');
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(map);

  const data = {geojson_str};
  const layer = L.geoJSON(data, {{
    style: function(feature) {{
      const kind = feature.properties.kind;
      if (kind === 'target') return {{ color: '#1565c0', weight: 3, fillOpacity: 0.3 }};
      if (kind === 'used') return {{ color: '#2e7d32', weight: 2, fillOpacity: 0.4 }};
      if (kind === 'excluded') return {{ color: '#888', weight: 1, fillOpacity: 0.15 }};
      if (kind === 'hookline') return {{ color: '#2e7d32', weight: 2, dashArray: '4,4' }};
      return {{}};
    }},
    pointToLayer: function(feature, latlng) {{
      return L.circleMarker(latlng, {{ radius: 8, color: '#c62828', fillColor: '#c62828', fillOpacity: 1 }});
    }},
    onEachFeature: function(feature, layer) {{
      if (feature.properties.popup) layer.bindPopup(feature.properties.popup);
    }}
  }}).addTo(map);

  if (layer.getBounds().isValid()) {{
    map.fitBounds(layer.getBounds(), {{ padding: [30, 30] }});
  }} else {{
    map.setView([44, -71.5], 12);
  }}
</script>"""


def write_html_report(result: dict, path: str):
    listing = result["listing"]
    addr = listing.get("addressLine1", "Unknown")
    error = result.get("error")

    rows_html = ""
    for c in result.get("candidates", []):
        used_class = ' class="used"' if c["used"] else ""
        value_str = f"${c['value']:,.0f}" if c["value"] is not None else "no VGSI match"
        rows_html += f"""
        <tr{used_class}>
          <td>{c['address']}</td><td>{c['land_use_desc'] or '?'}</td><td>{c['acres']}</td>
          <td>{'Yes' if c['size_ok'] else 'No'}</td><td>{value_str}</td>
          <td>{c['found_via']}</td><td>{'USED' if c['used'] else ''}</td>
        </tr>"""

    if error is None:
        summary_html = f"""
        <div class="summary">
          <p><b>Comp median:</b> ${result['comp_median']:,.0f} ({result['comp_count']} comps used)</p>
          <p><b>Listing price:</b> ${result['price']:,.0f}</p>
          <p><b>Gap:</b> <span class="{'gap-pos' if result['gap'] >= 0 else 'gap-neg'}">
             ${result['gap']:,.0f} ({result['gap_pct']:+.1f}%)</span></p>
        </div>"""
    else:
        summary_html = f"<div class='summary'><p><b>Could not compute a gap:</b> {error}</p></div>"

    tav = result.get("target_assessed_value")
    tav_str = f"${tav:,.0f}" if tav else "no VGSI match"
    map_html = build_map_html(build_map_geojson(result)) if result.get("target_geometry") else ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{addr} — ValueGap Report</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 2em auto; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  .target-card {{ background: #f5f5f5; border-radius: 8px; padding: 1em; margin-bottom: 1.5em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 0.9em; }}
  th {{ background: #eee; }}
  tr.used {{ background: #e8f5e9; font-weight: 600; }}
  .gap-pos {{ color: #1b5e20; font-weight: bold; }}
  .gap-neg {{ color: #b71c1c; font-weight: bold; }}
  .summary {{ margin: 1em 0; }}
  .reminder {{ font-size: 0.85em; color: #666; margin-top: 2em; border-top: 1px solid #ddd; padding-top: 1em; }}
</style></head><body>
<h1>{addr}</h1>
<div class="target-card">
  <p><b>Price:</b> ${listing.get('price', 0):,.0f} | <b>Type:</b> {listing.get('propertyType')} |
     <b>Sqft:</b> {listing.get('squareFootage', '?')} | <b>Built:</b> {listing.get('yearBuilt', '?')}</p>
  <p><b>Target's own assessed value:</b> {tav_str}</p>
</div>
{summary_html}
{map_html}
<h2>Comp candidates ({len(result.get('candidates', []))})</h2>
<table>
  <tr><th>Address</th><th>Land Use</th><th>Acres</th><th>Size OK</th><th>Assessed Value</th><th>Found Via</th><th></th></tr>
  {rows_html}
</table>
<p class="reminder">This is a lead, not a verdict. Rows highlighted green (on the map and in the table)
were used in the gap calculation; gray/unmarked ones are shown for context but excluded (wrong type,
size mismatch, or no VGSI match). Click any shape on the map for details.</p>
</body></html>"""

    with open(path, "w") as f:
        f.write(html)


def write_kml_report(result: dict, path: str):
    listing = result["listing"]
    addr = listing.get("addressLine1", "Unknown")
    lat, lon = listing.get("latitude"), listing.get("longitude")

    placemarks = [f"""
    <Placemark>
      <name>{addr} (LISTING)</name>
      <description>${listing.get('price', 0):,.0f} | {listing.get('propertyType')}</description>
      <Style><IconStyle><color>ff0000ff</color><scale>1.3</scale></IconStyle></Style>
      <Point><coordinates>{lon},{lat},0</coordinates></Point>
    </Placemark>"""]

    lines = []
    for c in result.get("candidates", []):
        color = "ff00ff00" if c["used"] else "ff888888"
        value_str = f"${c['value']:,.0f}" if c["value"] is not None else "no VGSI match"
        placemarks.append(f"""
    <Placemark>
      <name>{c['address']}</name>
      <description>{c['land_use_desc'] or '?'} | {value_str} | {'USED' if c['used'] else 'shown for context'}</description>
      <Style><IconStyle><color>{color}</color></IconStyle></Style>
      <Point><coordinates>{c['lon']},{c['lat']},0</coordinates></Point>
    </Placemark>""")
        if c["used"] and lat is not None:
            lines.append(f"""
    <Placemark>
      <name>hookline: {addr} -> {c['address']}</name>
      <Style><LineStyle><color>ff00ff00</color><width>2</width></LineStyle></Style>
      <LineString><coordinates>{lon},{lat},0 {c['lon']},{c['lat']},0</coordinates></LineString>
    </Placemark>""")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{addr} — ValueGap comps</name>
  {''.join(placemarks)}
  {''.join(lines)}
</Document>
</kml>"""

    with open(path, "w") as f:
        f.write(kml)


def write_reports(result: dict, out_dir: str):
    slug = slugify(result["listing"].get("addressLine1"))
    write_json_report(result, os.path.join(out_dir, f"{slug}.json"))
    write_html_report(result, os.path.join(out_dir, f"{slug}.html"))
    write_kml_report(result, os.path.join(out_dir, f"{slug}.kml"))
    return slug


# ---------------------------------------------------------------------------
# Rank mode
# ---------------------------------------------------------------------------

def run_rank_mode(groups: dict, out_path: str, reports_dir: str | None):
    ranked, no_comps = [], []
    results_by_address = {}

    for lid, group in groups.items():
        result = compute_gap_for_group(group)
        if result is None:
            continue
        addr = result["listing"].get("addressLine1")
        results_by_address[addr] = result
        if result["error"] == "no_comps":
            no_comps.append(addr)
        else:
            ranked.append({
                "address": addr, "property_type": result["listing"].get("propertyType"),
                "year_built": result["listing"].get("yearBuilt"),
                "target_assessed_value": result.get("target_assessed_value"),
                "recent_sale_price": None,
                "price": result["price"], "comp_median": result["comp_median"],
                "comp_count": result["comp_count"], "comp_min": result["comp_min"],
                "comp_max": result["comp_max"], "gap": result["gap"], "gap_pct": result["gap_pct"],
            })

    land_ranked = sorted((r for r in ranked if r["property_type"] == "Land"), key=lambda r: r["gap"], reverse=True)
    sfh_ranked = sorted((r for r in ranked if r["property_type"] == "Single Family"), key=lambda r: r["gap"], reverse=True)

    def add_relative_gap(rows):
        if not rows:
            return
        pcts = sorted(r["gap_pct"] for r in rows)
        n = len(pcts)
        gmed = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2
        for r in rows:
            r["relative_gap_pct"] = r["gap_pct"] - gmed

    add_relative_gap(land_ranked)
    add_relative_gap(sfh_ranked)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "address", "property_type", "year_built", "target_assessed_value", "recent_sale_price",
            "price", "comp_median", "comp_count", "comp_min", "comp_max", "gap", "gap_pct", "relative_gap_pct",
        ])
        writer.writeheader()
        writer.writerows(land_ranked)
        writer.writerows(sfh_ranked)

    print(f"Listings in combined file: {len(groups)}")
    print(f"Ranked (usable gap computed): {len(ranked)} ({len(land_ranked)} Land, {len(sfh_ranked)} Single Family)")
    print(f"No usable comps: {len(no_comps)}")
    for a in no_comps:
        print(f"  {a}")
    print()
    print(f"Wrote {out_path} -- Land and Single Family each sorted largest gap to smallest, Land block first")

    def print_section(title, rows):
        print()
        print(f"=== {title} ({len(rows)}) ===")
        print(f"{'Address':<28} {'Built':>6} {'TargetVal':>11} {'Price':>12} {'CompMedian':>12} {'Comps':>6} {'Gap':>12} {'Gap%':>8} {'RelGap%':>8}")
        print("-" * 120)
        for r in rows[:15]:
            built = r["year_built"] if r["year_built"] else "?"
            tav = f"${r['target_assessed_value']:,.0f}" if r["target_assessed_value"] else "no match"
            new_flag = "  <- CHECK: newer construction, assessment may lag" if (r["year_built"] and r["year_built"] >= 2020) else ""
            print(f"{r['address']:<28} {built:>6} {tav:>11} ${r['price']:>10,.0f} "
                  f"${r['comp_median']:>10,.0f} {r['comp_count']:>6} "
                  f"${r['gap']:>10,.0f} {r['gap_pct']:>7.1f}% {r['relative_gap_pct']:>7.1f}%{new_flag}")
        if len(rows) > 15:
            print(f"... and {len(rows) - 15} more in {out_path}")

    print_section("Land -- largest gap first", land_ranked)
    print_section("Single Family -- largest gap first", sfh_ranked)

    if reports_dir:
        os.makedirs(reports_dir, exist_ok=True)
        print()
        print(f"Writing detail reports (json/html/kml) for all {len(ranked) + len(no_comps)} listings into {reports_dir}/ ...")
        for addr in [r["address"] for r in land_ranked + sfh_ranked] + no_comps:
            slug = write_reports(results_by_address[addr], reports_dir)
            print(f"  {addr} -> {slug}.json / .html / .kml")

    print()
    print("Reminder: this is a lead list, not a verdict.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("combined_geojson", help="find_abutters.py's *_combined.geojson output")
    parser.add_argument("address", nargs="?", help="A specific listing's address (omit if using --rank)")
    parser.add_argument("--rank", metavar="OUTPUT_CSV", help="Rank every listing instead of one; writes to this CSV")
    parser.add_argument("--reports", help="Directory to write JSON/HTML/KML detail reports into")
    args = parser.parse_args()

    features = load_json(args.combined_geojson)["features"]
    groups = group_by_listing(features)

    if args.rank:
        run_rank_mode(groups, args.rank, args.reports)
        return

    if not args.address:
        print("Usage:")
        print("  python compute_gap.py <combined.geojson> \"<address>\" [--reports <dir>]")
        print("  python compute_gap.py <combined.geojson> --rank <output.csv> [--reports <dir>]")
        sys.exit(1)

    match = next((g for g in groups.values() if g["listing_point"] and
                  g["listing_point"]["properties"].get("listing_address", "").strip().lower() == args.address.strip().lower()), None)
    if match is None:
        print(f"No listing found with address == {args.address!r} in {args.combined_geojson}")
        sys.exit(1)

    result = compute_gap_for_group(match)
    print_report(result)
    if args.reports:
        os.makedirs(args.reports, exist_ok=True)
        slug = write_reports(result, args.reports)
        print()
        print(f"Wrote {slug}.json / .html / .kml into {args.reports}/")


if __name__ == "__main__":
    main()