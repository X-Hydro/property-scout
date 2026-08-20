"""
GRANIT parcel downloader — ValueGap

Pulls parcel boundaries + property type (no dollar values — GRANIT doesn't
publish those) for a given NH town from the state's public ArcGIS REST
service, and writes them to a GeoJSON file.

Source: NH GRANIT / NH Dept. of Revenue Administration CAMA-linked
parcel mosaic, layer "Parcels" (1) of the ParcelMosaic MapServer.
No auth required, no rate limiting observed — it's a public state GIS service.

Usage:
    python granit_parcel_downloader.py "LINCOLN" lincoln_parcels.geojson
    python granit_parcel_downloader.py "WOODSTOCK" woodstock_parcels.geojson
"""

import sys
import json
import time
import requests

BASE_URL = "https://nhgeodata.unh.edu/nhgeodata/rest/services/CAD/ParcelMosaic/MapServer/1/query"

# Fields available on this layer (confirmed via the service's metadata):
#   PID, Town, StreetAddress, SLU, SLUC, SLUM, NH_GIS_ID, DisplayId, Name, ...
# SLU/SLUC = state land use code — this is our "property type" signal.
OUT_FIELDS = "PID,Town,StreetAddress,SLU,SLUC,DisplayId,NH_GIS_ID,Name"

PAGE_SIZE = 2000  # server's MaxRecordCount


def fetch_town_parcels(town_name: str) -> list[dict]:
    """Page through all parcels for a given town and return GeoJSON features."""
    features = []
    offset = 0

    while True:
        params = {
            "where": f"UPPER(Town) = '{town_name.upper()}'",
            "outFields": OUT_FIELDS,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "returnGeometry": "true",
            # GRANIT's native SRS is NH State Plane (EPSG:3437-ish / wkid 102710).
            # Ask for WGS84 lat/lon directly so this drops straight into a
            # standard geocoding/PostGIS pipeline without a reprojection step.
            "outSR": 4326,
        }
        resp = requests.get(BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"GRANIT API error: {data['error']}")

        batch = data.get("features", [])
        features.extend(batch)
        print(f"  fetched {len(batch)} parcels (offset {offset}, total so far {len(features)})")

        if len(batch) < PAGE_SIZE:
            break  # last page
        offset += PAGE_SIZE
        time.sleep(0.2)  # be a polite citizen of a free public service

    return features


def main():
    if len(sys.argv) != 3:
        print("Usage: python granit_parcel_downloader.py <TOWN_NAME> <output.geojson>")
        sys.exit(1)

    town_name, out_path = sys.argv[1], sys.argv[2]
    print(f"Downloading parcels for {town_name}, NH...")
    features = fetch_town_parcels(town_name)

    geojson = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w") as f:
        json.dump(geojson, f)

    print(f"Wrote {len(features)} parcels to {out_path}")


if __name__ == "__main__":
    main()