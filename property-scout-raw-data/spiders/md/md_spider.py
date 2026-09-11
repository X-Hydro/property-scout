"""
Maryland spider — Property Values Database

Source: MD_ParcelBoundaries, Maryland Department of Planning (MDP) /
State Department of Assessments and Taxation (SDAT) joint statewide
parcel layer, all 24 jurisdictions (23 counties + Baltimore City) in one
service. Same shape as CT (single state agency, single queryable layer,
parcel geometry pre-joined to assessed-value attributes) -- no per-town
scraping, no vendor fragmentation.

    https://geodata.md.gov/imap/rest/services/PlanningCadastre/MD_ParcelBoundaries/MapServer/0/query

CONFIRMED FIELD NAMES -- from the layer's own live metadata (a real
schema fetch, not documentation prose), same discipline as CT/MA/NH.
NOT yet confirmed: an actual sample row's field VALUES (date-field
formats especially -- see note on MDPVDATE/SDATDATE/TRADATE below).
Run this once against a real jurisdiction and diff the printed
[schema check] + a sample record before trusting it at scale.

  ACCTID, JURSCODE, ADDRESS, STRTNUM, STRTDIR, STRTNAM, STRTTYP, STRTSFX,
  CITY, ZIPCODE, LU, DESCLU, ACRES, NFMLNDVL, NFMIMPVL, NFMTTLVL,
  YEARBLT, SQFTSTRC, TRADATE, CONSIDR1, POLYID

IMPORTANT GAPS / UNKNOWNS, flagged rather than guessed at:

  - JURSCODE is a 4-char jurisdiction CODE, not a county name (per the
    layer's own field alias "Jurisdiction Code"). CONFIRMED live
    2026-08-24 (see JURSCODE_TO_COUNTY below) -- all 24 codes and their
    county names are known. fetch_town() still takes a JURSCODE, not a
    friendly name (e.g. "BACO", not "Baltimore County") -- county name
    is populated on output records via the lookup table, but the CLI/
    run_ingest.py --towns argument itself is still code-based.
  - Base URL confirmed live 2026-08-24 via mdgeodata.md.gov (NOT
    geodata.md.gov, which was down for maintenance at the time -- if
    mdgeodata.md.gov ever goes down too, try geodata.md.gov as a
    fallback, they may be separate hosts serving the same backend).
  - MDPVDATE/SDATDATE/POLYDATE are typed as short (6-8 char) STRINGS,
    not esriFieldTypeDate, unlike TRADATE which is also a string. Every
    date-shaped field on this layer is a string, not a native Esri
    date -- format unconfirmed (could be YYYYMMDD, could be Julian
    YYYYDDD given the odd 7-char lengths on MDPVDATE/SDATDATE/POLYDATE).
    _md_date_to_iso() below handles the 8-char YYYYMMDD case only (same
    shape as CT's Sale_Date once epoch-converted, NJ's DEED_DATE) and
    returns None for anything else rather than guessing -- confirm
    against a real value before trusting last_sale_date.
  - No bedrooms/bathrooms on this layer. A separate
    MD_ComputerAssistedMassAppraisal service exists in the same
    PlanningCadastre folder and may have deeper CAMA detail -- not
    investigated yet, out of scope for this first pass.
  - property_type: DESCLU (Land Use Description) passed through
    standardize_property_type() same as every other state spider. No
    real DESCLU values have been confirmed yet, so -- same as CT's
    State_Use_Description at first -- every value will come back
    unchanged (raw MD text) until real values are seen and added to
    that shared CSV.

Usage:
    python -m spiders.md_spider --list-jurisdictions
    python -m spiders.md_spider 03 --out data/          # Baltimore County, code unconfirmed -- see above
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
    "https://mdgeodata.md.gov/imap/rest/services/PlanningCadastre/"
    "MD_ParcelBoundaries/MapServer/0/query"
)
PAGE_SIZE = 1000  # MaxRecordCount confirmed from live layer metadata
SOURCE_TAG = "MD_MDP_SDAT_ParcelBoundaries"

# CONFIRMED live via a real query (2026-08-24): all 24 JURSCODE values.
# geodata.md.gov (the original base URL) was down for maintenance at the
# time -- mdgeodata.md.gov is the host that's actually live; swap back
# if that changes, but this one worked from a real curl run.
#
# Name mapping below is NOT a guess at data -- the codes themselves are
# live-confirmed, and MD's 24 jurisdictions are public knowledge, so this
# is just labeling already-real codes (first-4-letters convention, e.g.
# PRIN -> Prince George's), same spirit as CT/MA's Town_Name being
# self-explanatory once confirmed live. BACI/BACO split Baltimore City
# from Baltimore County.
JURSCODE_TO_COUNTY = {
    "ALLE": "Allegany", "ANNE": "Anne Arundel", "BACI": "Baltimore City",
    "BACO": "Baltimore County", "CALV": "Calvert", "CARO": "Caroline",
    "CARR": "Carroll", "CECI": "Cecil", "CHAR": "Charles",
    "DORC": "Dorchester", "FRED": "Frederick", "GARR": "Garrett",
    "HARF": "Harford", "HOWA": "Howard", "KENT": "Kent",
    "MONT": "Montgomery", "PRIN": "Prince George's", "QUEE": "Queen Anne's",
    "SOME": "Somerset", "STMA": "St. Mary's", "TALB": "Talbot",
    "WASH": "Washington", "WICO": "Wicomico", "WORC": "Worcester",
}


def _num(attrs: dict, field: str):
    v = attrs.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _md_date_to_iso(raw) -> str | None:
    """Handles the plain 8-char YYYYMMDD case only. MD's date-shaped
    fields are untyped strings of varying/odd length (see module
    docstring) -- returns None rather than guessing at any other shape."""
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None  # unconfirmed format (e.g. the 6/7-char fields) -- don't guess


def _geojson_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """Same rough-centroid approach as ct_spider.py."""
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


def _build_address(attrs: dict) -> str | None:
    parts = [attrs.get("STRTNUM"), attrs.get("STRTDIR"), attrs.get("STRTNAM"),
              attrs.get("STRTTYP"), attrs.get("STRTSFX")]
    parts = [str(p).strip() for p in parts if p not in (None, "", " ")]
    built = " ".join(parts) if parts else None
    return built or attrs.get("ADDRESS")  # fall back to the pre-built field


class MDSpider(StateSpider):
    state_code = "MD"

    def __init__(self):
        self._schema_diagnostic_printed = False

    def list_towns(self) -> list[str]:
        """Returns JURSCODE values (NOT county names -- see module
        docstring). Same self-updating rationale as CTSpider.list_towns():
        query the service's own distinct values instead of hardcoding a
        code->name table this project hasn't confirmed live yet."""
        params = {
            "where": "1=1",
            "outFields": "JURSCODE",
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
        codes = sorted({f["attributes"]["JURSCODE"] for f in data.get("features", [])
                         if f["attributes"].get("JURSCODE")})
        return codes

    def _query_page(self, jurscode: str, offset: int) -> dict:
        safe = jurscode.replace("'", "''")
        where = f"JURSCODE = '{safe}'"
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "geojson",  # confirmed supported ("Supported Query Formats: JSON, geoJSON")
        }
        url = f"{BASE_QUERY_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PropertyValuesDB research tool"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
        data = json.loads(body)
        if "error" in data:
            raise SpiderError(f"ArcGIS query error at offset={offset}: {data['error']}")
        return data

    def _normalize_feature(self, feature: dict, jurscode: str) -> dict:
        attrs = feature.get("properties", {})
        geometry = feature.get("geometry")
        lat, lon = _geojson_centroid(geometry)

        acctid = attrs.get("ACCTID")
        record = {
            "property_id": f"MD:{jurscode}:{acctid}" if acctid else None,
            "state": "MD",
            "county": JURSCODE_TO_COUNTY.get(jurscode, jurscode),
            "municipality": None,  # no confirmed municipality field distinct from JURSCODE/CITY
            "parcel_id": acctid,
            "address": _build_address(attrs),
            "city": attrs.get("CITY"),
            "zip": attrs.get("ZIPCODE"),
            "latitude": lat,
            "longitude": lon,
            "acreage": _num(attrs, "ACRES"),
            "assessed_value": _num(attrs, "NFMTTLVL"),
            "assessed_land_value": _num(attrs, "NFMLNDVL"),
            "assessed_building_value": _num(attrs, "NFMIMPVL"),
            "assessment_year": None,  # SDATDATE exists but format unconfirmed -- see docstring
            "last_sale_price": _num(attrs, "CONSIDR1"),
            "last_sale_date": _md_date_to_iso(attrs.get("TRADATE")),
            "building_sqft": _num(attrs, "SQFTSTRC"),
            "bedrooms": None,  # not on this layer -- see docstring
            "bathrooms": None,  # not on this layer -- see docstring
            "year_built": _num(attrs, "YEARBLT"),
            "property_type": standardize_property_type(attrs.get("DESCLU")),
            "source": SOURCE_TAG,
            "source_url": BASE_QUERY_URL,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        return record

    def fetch_town(self, jurscode: str) -> list[dict]:
        """NOTE: takes a JURSCODE (e.g. '03'), not a county name -- see
        module docstring. run_ingest.py's --towns argument name is
        therefore misleading for MD until a code->name table is
        confirmed and this is wrapped with a friendly lookup."""
        records = []
        offset = 0
        while True:
            page = self._query_page(jurscode, offset)
            features = page.get("features", [])
            if not self._schema_diagnostic_printed and features:
                real_keys = sorted(features[0]["properties"].keys())
                print(f"  [schema check] MD field names: {real_keys}")
                print(f"  [schema check] sample record: {features[0]['properties']}")
                self._schema_diagnostic_printed = True
            for feature in features:
                records.append(self._normalize_feature(feature, jurscode))
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("jurscodes", nargs="*", help="MD JURSCODE values, e.g. 03 (code meaning unconfirmed)")
    parser.add_argument("--list-jurisdictions", action="store_true",
                         help="print all distinct JURSCODE values found live, then exit")
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    spider = MDSpider()

    if args.list_jurisdictions:
        codes = spider.list_towns()
        print(f"{len(codes)} distinct JURSCODE values found:")
        for c in codes:
            print(f"  {c}  ({JURSCODE_TO_COUNTY.get(c, 'unknown')})")
        return

    if not args.jurscodes:
        print("ERROR: pass one or more JURSCODE values, or use --list-jurisdictions first")
        sys.exit(1)

    spider.run(args.jurscodes, args.out)


if __name__ == "__main__":
    main()