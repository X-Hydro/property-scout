"""
Massachusetts spider — Property Values Database

REWRITTEN as a real live query, same architecture as ct_spider.py --
replaces the earlier file-based version, which required manually
downloading a GeoJSON per town via ArcGIS Hub (confirmed impractical for
351 municipalities). The documented FeatureServer URL that motivated the
original file-based design was genuinely stale (MassGIS's own page flags
"NEW REST URL as of 4/2/2026"); the current one is confirmed live and
queryable:

    https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/
    Massachusetts_Property_Tax_Parcels/FeatureServer/0/query

CONFIRMED LIVE (via a real query response, not documentation):
  - CITY is a genuine per-parcel field (confirmed real value: "SOMERSET")
    -- queryable by town name directly, same role as CT's Town_Name.
  - supportedQueryFormats is "JSON" only at this layer (unlike CT, which
    supports geoJSON) -- this uses f=json (Esri JSON) with outSR=4326 for
    WGS84 reprojection, and converts ring geometry to GeoJSON manually,
    same pattern as ct_spider.py's ORIGINAL first attempt before CT's
    layer-level geoJSON support was found.
  - MaxRecordCount is 2000, exceededTransferLimit was true on an
    unfiltered sample query -- pagination required, same as CT/NH.
  - All field names below are confirmed from a real returned record
    (MAP_PAR_ID, LOC_ID, TOWN_ID, PROP_ID, BLDG_VAL, LAND_VAL, TOTAL_VAL,
    FY, LOT_SIZE, LOT_UNITS, LS_DATE, LS_PRICE, ADDR_NUM, FULL_STR,
    LOCATION, CITY, ZIP, YEAR_BUILT, BLD_AREA, RES_AREA, USE_DESC), not
    guessed from prose documentation -- avoiding the exact mismatch class
    that broke CT's first attempt (Town Name vs Town_Name).

REAL WRINKLE, confirmed via the sample record: LOT_SIZE's unit is NOT
fixed -- LOT_UNITS ("Acres" confirmed; square-feet variant unconfirmed,
handled defensively) varies per record, unlike the earlier file-based
version where every value was uniformly square feet. Converting without
checking LOT_UNITS per-row would have been wrong.

FIELD MAPPING GAPS (confirmed absent from this layer's real field list,
not a naming guess): bedrooms, bathrooms. county is also absent, same as
CT and the earlier file-based MA version.

property_type: USE_DESC is passed through spiders/common/property_types.py's
standardize_property_type() -- same shared mapping every other state
spider uses, e.g. "Single Family Residential" -> "Single Family",
"Developable Residential Land" -> "Vacant Land". An unrecognized
USE_DESC value is left unchanged, not guessed at -- see that module's
docstring for how to add a new alias.

building_sqft: prefers RES_AREA (documented as primarily for 1-3 family
homes) over BLD_AREA (primarily apartment/commercial) when both exist,
since most single-family comps care about RES_AREA specifically -- falls
back to BLD_AREA if RES_AREA is null.

Usage:
    python -m spiders.ma.ma_spider Andover --out data/
"""

import sys
import json
import argparse
from datetime import date
import urllib.request
import urllib.parse

from ..common.base import StateSpider, SpiderError
from ..common.property_types import standardize_property_type

BASE_QUERY_URL = (
    "https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/"
    "Massachusetts_Property_Tax_Parcels/FeatureServer/0/query"
)
PAGE_SIZE = 2000
SOURCE_TAG = "MA_MassGIS_L3_Parcels_Live"


def _num(attrs: dict, field: str):
    v = attrs.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _esri_rings_to_geojson(geometry: dict | None) -> dict | None:
    """Same approach as ct_spider.py's original (pre-f=geojson) version --
    this layer doesn't support f=geojson (confirmed: supportedQueryFormats
    is "JSON" only), so geometry has to be converted manually. Only
    polygon rings are expected for a parcel layer."""
    if not geometry or "rings" not in geometry:
        return None
    return {"type": "Polygon", "coordinates": geometry["rings"]}


def _esri_ring_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    if not geometry or "rings" not in geometry or not geometry["rings"]:
        return None, None
    xs, ys = [], []
    for ring in geometry["rings"]:
        for pt in ring:
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return None, None
    return sum(ys) / len(ys), sum(xs) / len(xs)  # (lat, lon)


def _acres(lot_size, lot_units) -> float | None:
    """LOT_UNITS varies PER RECORD (confirmed real data) -- must check
    it every time, not assume one unit globally like the earlier
    file-based version could get away with."""
    if lot_size is None:
        return None
    if not lot_units:
        return None  # unknown unit -- don't guess, matches project convention
    unit = lot_units.strip().lower()
    if unit.startswith("acre"):
        return lot_size
    if "sf" in unit or "sq" in unit or "feet" in unit:
        return lot_size / 43560.0
    return None  # unrecognized unit string -- flagged, not guessed


def _yyyymmdd_to_iso(raw) -> str | None:
    """LS_DATE confirmed real format: 'YYYYMMDD' string (e.g. '20060802'),
    matching MassGIS's own documented spec -- different from the earlier
    file-based version's already-ISO SALE_DATE."""
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) != 8 or not s.isdigit():
        return None
    if s == "00000000":
        return None  # plausible zero-date sentinel, same category as CT's 1899 sentinel
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _build_address(attrs: dict) -> str | None:
    addr_num = attrs.get("ADDR_NUM")
    full_str = attrs.get("FULL_STR")
    location = attrs.get("LOCATION")
    parts = [p for p in (addr_num, full_str) if p not in (None, "", " ")]
    addr = " ".join(str(p).strip() for p in parts) if parts else None
    if location and str(location).strip():
        addr = f"{addr} {location.strip()}" if addr else location.strip()
    return addr or attrs.get("SITE_ADDR")  # fall back to the pre-built full string field


class MASpider(StateSpider):
    state_code = "MA"

    def __init__(self):
        self._schema_diagnostic_printed = False

    def list_towns(self) -> list[str]:
        """Same rationale as CTSpider.list_towns() -- query the service's
        own distinct CITY values instead of hardcoding 351 town names."""
        params = {
            "where": "1=1",
            "outFields": "CITY",
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "f": "json",
        }
        url = f"{BASE_QUERY_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PropertyValuesDB research tool"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            raise SpiderError(f"ArcGIS list_towns query error: {data['error']}")
        towns = sorted({f["attributes"]["CITY"] for f in data.get("features", [])
                         if f["attributes"].get("CITY")})
        return towns

    def _query_page(self, town: str, offset: int) -> dict:
        safe_town = town.replace("'", "''")
        where = f"UPPER(CITY) = UPPER('{safe_town}')"
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "json",  # NOT geojson -- unsupported at this layer, see module docstring
        }
        url = f"{BASE_QUERY_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PropertyValuesDB research tool"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
        data = json.loads(body)
        if "error" in data:
            raise SpiderError(f"ArcGIS query error at offset={offset}: {data['error']}")
        return data

    def _normalize_feature(self, feature: dict, town: str) -> dict:
        attrs = feature.get("attributes", {})
        geometry = feature.get("geometry")
        lat, lon = _esri_ring_centroid(geometry)

        loc_id = attrs.get("LOC_ID")
        res_area = _num(attrs, "RES_AREA")
        bld_area = _num(attrs, "BLD_AREA")

        record = {
            "property_id": f"MA:{loc_id}" if loc_id else None,
            "state": "MA",
            "county": None,
            "municipality": attrs.get("CITY") or town,
            "parcel_id": loc_id,
            "address": _build_address(attrs),
            "city": attrs.get("CITY") or town,
            "zip": attrs.get("ZIP"),
            "latitude": lat,
            "longitude": lon,
            "acreage": _acres(_num(attrs, "LOT_SIZE"), attrs.get("LOT_UNITS")),
            "assessed_value": _num(attrs, "TOTAL_VAL"),
            "assessed_land_value": _num(attrs, "LAND_VAL"),
            "assessed_building_value": _num(attrs, "BLDG_VAL"),
            "assessment_year": attrs.get("FY"),
            "last_sale_price": _num(attrs, "LS_PRICE"),
            "last_sale_date": _yyyymmdd_to_iso(attrs.get("LS_DATE")),
            "building_sqft": res_area if res_area is not None else bld_area,
            "bedrooms": None,
            "bathrooms": None,
            "year_built": _num(attrs, "YEAR_BUILT"),
            "property_type": standardize_property_type(attrs.get("USE_DESC")),
            "source": SOURCE_TAG,
            "source_url": BASE_QUERY_URL,
            "source_date": date.today().isoformat(),
            "_geometry": _esri_rings_to_geojson(geometry),
        }
        return record

    def fetch_town(self, town: str) -> list[dict]:
        records = []
        offset = 0
        while True:
            page = self._query_page(town, offset)
            features = page.get("features", [])
            if not self._schema_diagnostic_printed and features:
                real_keys = sorted(features[0]["attributes"].keys())
                print(f"  [schema check] MA field names: {real_keys}")
                self._schema_diagnostic_printed = True
            for feature in features:
                records.append(self._normalize_feature(feature, town))
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="+", help="MA municipality names, e.g. Andover")
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    spider = MASpider()
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()