"""
Connecticut spider — Property Values Database

Source: Connecticut_CAMA_and_Parcel_Layer_2024 FeatureServer, CT's
current OPM CAMA+Parcel service (legally mandated under CGS Sec.
7-100l, all 169 municipalities). Standard ArcGIS REST Feature Service,
queried via HTTP GET, paginated via resultOffset/resultRecordCount
(server-side max 2000 records/request).

NOTE: an OLDER OPM service, Connecticut_State_Parcel_Layer_2023, is a
different, stale (2023) vintage of the same underlying program -- this
spider deliberately points at the newer 2024+ service instead, since
its field schema was independently confirmed live (via a third-party
reference site's documented curl example, last verified 2026-06-20)
and its underlying CT OPM catalog entry shows updates as recent as June
2026 (adding Property_Zip/Mailing_Zip fields). If CT ever publishes a
distinctly-named newer service (e.g. a "_2025" or "_2026" suffix),
re-verify BASE_QUERY_URL against it -- this project has already been
burned once by pointing at a resource that quietly became outdated
(NH's own PID-range scraping problem was a version of the same lesson).

CONFIRMED FIELD NAMES — complete, from a real live run (2 CT towns,
49,214 total records, zero errors). This replaces every previous guess:

  Parcel_ID, Town_Name, Location_1, Property_City, Land_Acres,
  Assessed_Total, Assessed_Land, Assessed_Building, Valuation_Year,
  Sale_Price, Sale_Date, Living_Area, Number_of_Bedroom,
  Number_of_Baths, Number_of_Half_Baths, ayb (actual year built),
  State_Use_Description, Planning_Region

Two corrections vs. earlier guesses, both confirmed wrong by the live
field list:
  - Property_Zip / Mailing_Zip do NOT exist in this live schema, despite
    CT's own June 2026 catalog note claiming they'd been added -- either
    that hasn't shipped to this specific service yet, or refers to a
    different one. Live evidence wins over documentation here.
  - "Living_Are" (the earlier guess, carried over from the OLDER 2023
    service's docs, which had a typo) is actually "Living_Area" (no
    typo) on this 2024+ service.

REMAINING GAP: county. CT has no functioning county government;
Planning_Region IS a real, confirmed field, but it's a different concept
(a multi-town regional planning body, not a county) -- left unmapped
rather than treated as equivalent, though the raw field is available in
the source data if a future decision is made to use it anyway.

property_type: State_Use_Description is passed through
spiders/common/property_types.py's standardize_property_type() -- same
shared mapping NH and MA use. NONE of CT's actual State_Use_Description
values are in that CSV yet (no live run's real values have been
confirmed for this field the way Town_Name/Location_1/etc. were) -- so
until real values are seen and added, every CT property_type will come
back UNCHANGED (raw CT text), same as any other unrecognized value.
The "[schema check]" diagnostic below only prints field NAMES, not
values -- to find CT's actual State_Use_Description values, inspect a
few real output records after a run (or print attrs.get(
"State_Use_Description") directly during one), then add rows to the CSV.

Usage:
    python -m spiders.ct_spider "Bristol" "New Haven" --out data/
"""

import sys
import json
import argparse
from datetime import date, datetime, timezone
import urllib.request
import urllib.parse

from ..common.base import StateSpider, SpiderError
from ..common.property_types import standardize_property_type

BASE_QUERY_URL = (
    "https://services3.arcgis.com/3FL1kr7L4LvwA2Kb/arcgis/rest/services/"
    "Connecticut_CAMA_and_Parcel_Layer_2024/FeatureServer/0/query"
)
PAGE_SIZE = 2000
SOURCE_TAG = "CT_OPM_CAMA_2024"


def _geojson_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """Rough centroid (average of all vertex coordinates) for lat/lon
    convenience fields -- NOT a true polygon centroid (doesn't account
    for area weighting), sufficient for a rough map-pin location only.
    Works on real GeoJSON coordinates now that f=geojson is used."""
    if not geometry or "coordinates" not in geometry:
        return None, None
    coords = []

    def collect(c):
        if isinstance(c[0], (int, float)):
            coords.append(c)
        else:
            for sub in c:
                collect(sub)

    try:
        collect(geometry["coordinates"])
    except (KeyError, IndexError, TypeError):
        return None, None
    if not coords:
        return None, None
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _num(attrs: dict, field: str):
    v = attrs.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _epoch_ms_to_iso_date(value) -> str | None:
    """
    CT's date fields (Sale_Date, Prior_Sale_Date) come back as raw Esri
    epoch-millisecond integers, not ISO strings, even under f=geojson.
    Converts to a plain ISO date string. Also filters out the ~1899-12-30
    sentinel value CT uses for "no date recorded" (confirmed via a real
    returned value of -2209161600000ms == 1899-12-30 UTC) -- treating a
    date that old as a genuine sale date would be wrong; None is correct.
    """
    if value is None or value == "":
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if dt.year <= 1900:  # sentinel "no date" placeholder, not a real sale date
        return None
    return dt.date().isoformat()


def _combined_bathrooms(attrs: dict):
    full = _num(attrs, "Number_of_Baths")
    half = _num(attrs, "Number_of_Half_Baths")
    if full is None and half is None:
        return None
    return (full or 0) + 0.5 * (half or 0)


def _dedupe_property_ids(records: list[dict], town: str) -> list[dict]:
    """
    Parcel_ID is NOT always a real unique identifier -- CT uses shared
    placeholder text ("MISMATCH", blank) for parcels whose CAMA link
    failed, confirmed via real data (39 "CT:MISMATCH" rows, 16 blank, in
    one town alone). Silently keying on these would merge many different
    physical parcels into one DB row and lose the rest -- and even a
    genuine one-off duplicate breaks a batched Postgres upsert entirely
    (ON CONFLICT DO UPDATE can't affect the same row twice in one
    statement). Fix: any property_id seen more than once gets OBJECTID
    (CT's own guaranteed-unique internal row id) appended, so every
    record survives with a real distinct key, and the original
    (possibly-junk) id stays visible for debugging rather than hidden.
    """
    seen_counts: dict[str, int] = {}
    for r in records:
        pid = r["property_id"]
        seen_counts[pid] = seen_counts.get(pid, 0) + 1

    duplicated_ids = {pid for pid, n in seen_counts.items() if n > 1 and pid is not None}
    if duplicated_ids:
        print(f"  {town}: {len(duplicated_ids)} property_id value(s) were not unique "
              f"({sum(seen_counts[p] for p in duplicated_ids)} affected records) -- "
              f"disambiguated with OBJECTID, sample: {sorted(duplicated_ids)[:5]}")

    seen_so_far: set[str] = set()
    for r in records:
        pid = r["property_id"]
        if pid in duplicated_ids:
            oid = r.pop("_objectid", None)
            r["property_id"] = f"{pid}#OBJECTID{oid}" if oid else f"{pid}#{id(r)}"
        else:
            r.pop("_objectid", None)
    return records


class CTSpider(StateSpider):
    state_code = "CT"

    def __init__(self):
        self._schema_diagnostic_printed = False

    def list_towns(self) -> list[str]:
        """
        Queries the service's own distinct Town_Name values instead of
        needing a hardcoded 169-town list -- self-updating if CT ever
        adds/renames a town, and avoids maintaining a separate static
        list that could drift out of sync with the real data.
        """
        params = {
            "where": "1=1",
            "outFields": "Town_Name",
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
        towns = sorted({f["attributes"]["Town_Name"] for f in data.get("features", [])
                         if f["attributes"].get("Town_Name")})
        return towns

    def _query_page(self, town: str, offset: int) -> dict:
        # Real field name confirmed via layer metadata's "Display Field:
        # Town_Name" -- the prose docs' "Town Name" (with a space) is a
        # human-readable label, not the actual queryable field name, and
        # caused a real 400 error when used directly.
        safe_town = town.replace("'", "''")  # basic SQL-string escaping
        where = f"Town_Name = '{safe_town}'"
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "geojson",  # confirmed supported at the layer level
        }
        url = f"{BASE_QUERY_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PropertyValuesDB research tool"})
        # 120s, not 30s -- a full page (up to 2000 records, outFields=*,
        # full geometry) is a much heavier response than a quick manual
        # test query, and 30s wasn't enough headroom (confirmed via a
        # real timeout; a small 3-record manual browser test loaded fine,
        # isolating this as a payload-size/timeout issue, not a broken
        # query or a down server).
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
        data = json.loads(body)
        if "error" in data:
            raise SpiderError(
                f"ArcGIS query error at offset={offset}: {data['error']}"
            )
        return data

    def _normalize_feature(self, feature: dict, town: str) -> dict:
        # f=geojson response shape: feature["properties"] + feature["geometry"]
        # already real GeoJSON -- NOT feature["attributes"] (that was the
        # f=json/Esri-JSON shape from before the f=geojson fix).
        attrs = feature.get("properties", {})
        geometry = feature.get("geometry")
        lat, lon = _geojson_centroid(geometry)

        # All field names below are CONFIRMED from a real live run's
        # diagnostic field list -- no more guesses. See module docstring
        # for the two corrections vs. earlier guesses.
        parcel_id = attrs.get("Parcel_ID")
        record = {
            "property_id": f"CT:{parcel_id}" if parcel_id else None,
            "_objectid": attrs.get("OBJECTID"),  # internal use only -- for de-duping, stripped before output
            "state": "CT",
            "county": None,  # Planning_Region is available but not the same concept -- see docstring
            "municipality": attrs.get("Town_Name") or town,
            "parcel_id": parcel_id,
            "address": attrs.get("Location_1"),
            "city": attrs.get("Property_City") or attrs.get("Town_Name") or town,
            "zip": None,  # confirmed NOT in this live schema, despite CT's catalog note -- see docstring
            "latitude": lat,
            "longitude": lon,
            "acreage": _num(attrs, "Land_Acres"),
            "assessed_value": _num(attrs, "Assessed_Total"),
            "assessed_land_value": _num(attrs, "Assessed_Land"),
            "assessed_building_value": _num(attrs, "Assessed_Building"),
            "assessment_year": attrs.get("Valuation_Year"),
            "last_sale_price": _num(attrs, "Sale_Price"),
            "last_sale_date": _epoch_ms_to_iso_date(attrs.get("Sale_Date")),
            "building_sqft": _num(attrs, "Living_Area"),
            "bedrooms": _num(attrs, "Number_of_Bedroom"),
            "bathrooms": _combined_bathrooms(attrs),
            "year_built": _num(attrs, "ayb"),  # "actual year built" -- standard CAMA abbreviation
            "property_type": standardize_property_type(attrs.get("State_Use_Description")),
            "source": SOURCE_TAG,
            "source_url": BASE_QUERY_URL,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        return record

    def fetch_town(self, town: str) -> list[dict]:
        records = []
        offset = 0
        while True:
            page = self._query_page(town, offset)
            features = page.get("features", [])
            if not self._schema_diagnostic_printed and features:
                # Prints once per spider run (not once per town) -- kept
                # as an ongoing schema-drift detector, same philosophy as
                # AuctionScout's row-count QC checks, not a one-time debug
                # aid to delete once things work.
                real_keys = sorted(features[0]["properties"].keys())
                print(f"  [schema check] CT field names: {real_keys}")
                self._schema_diagnostic_printed = True
            for feature in features:
                records.append(self._normalize_feature(feature, town))
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return _dedupe_property_ids(records, town)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="+", help="CT municipality names, e.g. Bristol")
    parser.add_argument("--out", default="data", help="output directory for GeoJSON files")
    args = parser.parse_args()

    spider = CTSpider()
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()