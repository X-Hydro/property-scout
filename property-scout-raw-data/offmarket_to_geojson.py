"""
Converts a raw /search/offmarket sweep JSON file (e.g.
nh_offmarket_zip03251.json) into a proper point GeoJSON, loadable directly
in QGIS -- the raw file itself is NOT GeoJSON (see nh_spider.py/
join_parcels_offmarket.py's docstrings: it's the raw API response shape,
lat/lon nested under location.*, never written back out as geometry).

Every field join_parcels_offmarket.py itself uses (zpid, address,
zestimate, tax_assessed_value, tax_assessment_year, price.value) is kept
as a feature property, so you can style/filter by them directly in QGIS --
e.g. color points by whether tax_assessed_value is null, to see at a
glance which points would have fallen back to zestimate in the join.

Usage:
    python offmarket_to_geojson.py nh_test/offmarket-data/nh_offmarket_zip03251.json nh_offmarket_zip03251_points.geojson
"""

import sys
import json


def payload_to_geojson(payload: dict) -> dict:
    """Pure transform: raw /search/offmarket response -> GeoJSON
    FeatureCollection dict. No file I/O -- split out so offmarket_sweep.py
    can reuse this directly to write a debug geojson alongside each raw
    sweep file, without duplicating this logic or shelling out to this
    script."""
    features = []
    for rec in payload.get("offMarketResults", []):
        loc = rec.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            continue

        estimates = rec.get("estimates") or {}
        tax = rec.get("taxAssessment") or {}
        addr = rec.get("address") or {}
        price = rec.get("price") or {}

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "zpid": rec.get("zpid"),
                "address": addr.get("streetAddress"),
                "zestimate": estimates.get("zestimate"),
                "rent_zestimate": estimates.get("rentZestimate"),
                "tax_assessed_value": tax.get("taxAssessedValue"),
                "tax_assessment_year": tax.get("taxAssessmentYear"),
                "list_or_last_price": price.get("value"),
                "property_type": rec.get("propertyType"),
                "year_built": rec.get("yearBuilt"),
            },
        })

    return {"type": "FeatureCollection", "features": features}


def convert(in_path: str, out_path: str):
    with open(in_path) as f:
        payload = json.load(f)

    geojson = payload_to_geojson(payload)
    n_skipped = len(payload.get("offMarketResults", [])) - len(geojson["features"])

    with open(out_path, "w") as f:
        json.dump(geojson, f)

    print(f"{len(geojson['features'])} point(s) written to {out_path}"
          + (f" ({n_skipped} record(s) skipped, no coordinates)" if n_skipped else ""))


def main():
    if len(sys.argv) != 3:
        print("Usage: python offmarket_to_geojson.py <input.json> <output.geojson>")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()