"""
New Jersey spider — Property Values Database

Source: "Parcels and MOD-IV Composite of NJ", NJ Office of Information
Technology / Office of GIS (NJOGIS) statewide parcel layer, joined to
NJ Dept. of the Treasury's MOD-IV tax assessment records. Same shape as
CT/MA/MD: one state agency, one queryable layer, all counties/
municipalities, geometry pre-joined to assessed-value attributes.

    https://maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0/query

NOTE: this is the maps.nj.gov MapServer mirror. NJOGIS's own hosted
FeatureServer (services2.arcgis.com/XVOqAjTOJ5P6ngMu/.../Parcels_Composite_NJ_WM)
is described as the primary/current one in NJ's own "Retired Services"
notice (the old test-named service was retired 2026-03-11, this is its
replacement) -- both should carry the same underlying data since NJOGIS
publishes the composite once and mirrors it, but only the maps.nj.gov
layer's real field list has actually been confirmed live so far. Worth
spot-checking the FeatureServer mirror returns the same field names
before assuming they're interchangeable.

CONFIRMED FIELD NAMES -- from the layer's own live metadata (real schema
fetch, not documentation prose):

  PAMS_PIN, PIN_NODUP, PCL_MUN, COUNTY, MUN_NAME, PROP_LOC, ST_ADDRESS,
  CITY_STATE, ZIP_CODE, ZIP5, LAND_VAL, IMPRVT_VAL, NET_VALUE, CALC_ACRE,
  BLDG_DESC, LAND_DESC, PROP_CLASS, PROP_USE, BLDG_CLASS, DEED_DATE,
  YR_CONSTR, SALE_PRICE, DWELL, COMM_DWELL

IMPORTANT GAPS / UNKNOWNS -- most resolved 2026-08-24 via a real sample
record (Aberdeen Twp, Monmouth County), flagged here rather than guessed:

  - OWNER_NAME is redacted per Daniel's Law (NJ's home-address-privacy
    statute for certain protected classes, same law referenced in the
    California parcel research earlier this session) -- CONFIRMED blank
    ('') on the real sample record, not a bug.
  - DEED_DATE format CONFIRMED: YYMMDD (real sample '210127' = 2021-01-27,
    consistent with that record's populated SALE_PRICE/DEED_BOOK/
    DEED_PAGE). Two-digit year assumed 20YY -- see _nj_date_to_iso()
    docstring for why a split-century heuristic wasn't added.
  - property_type CORRECTED: LAND_DESC was wrongly assumed to be a use
    description in the first version of this spider -- the real sample
    value ('103X 165') is lot frontage x depth, not a description. The
    real field is PROP_CLASS (official NJ Division of Taxation MOD-IV
    classification code, e.g. '2' = Residential) -- decoded via
    NJ_PROP_CLASS_DESC below, then passed through
    standardize_property_type() same as every other state.
  - property_id now uses PIN_NODUP (confirmed real, present on the
    sample record) instead of PAMS_PIN -- the field's own name signals
    NJOGIS already de-duplicates it at the source, which may make this
    spider's version of CT/MD's _dedupe_property_ids() unnecessary. Not
    yet proven at scale across a full run -- if run_ingest.py ever logs
    an ON CONFLICT collision from this file, that assumption was wrong
    and the dedupe fix needs porting over after all.
  - No bedrooms/bathrooms/building_sqft field confirmed -- DWELL/
    COMM_DWELL are dwelling-unit COUNTS (small integers, both present
    but null in the sample residential record), not bedroom counts;
    left unmapped rather than guessed. YR_CONSTR confirmed populated
    (sample: 1945).
  - county: PCL_MUN is a 4-char municipality code (not human-readable);
    COUNTY and MUN_NAME are both confirmed real string fields (sample:
    COUNTY='MONMOUTH', MUN_NAME='ABERDEEN TWP') and used directly below.

Usage:
    python -m spiders.nj_spider --list-towns
    python -m spiders.nj_spider "Aberdeen Twp" --out data/   # confirmed real town name format, incl. "Twp" suffix
"""

import sys
import json
import argparse
from datetime import date
import urllib.request
import urllib.parse

from ..common.base import StateSpider, SpiderError
from ..common.property_types import standardize_property_type

BASE_QUERY_URL = "https://maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0/query"
PAGE_SIZE = 1000  # MaxRecordCount confirmed from live layer metadata
SOURCE_TAG = "NJ_NJOGIS_ParcelsModIVComposite"


def _num(attrs: dict, field: str):
    v = attrs.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nj_date_to_iso(raw) -> str | None:
    """DEED_DATE CONFIRMED live 2026-08-24: 6-char string, format YYMMDD
    (real sample: '210127' from a record with matching populated
    SALE_PRICE/DEED_BOOK/DEED_PAGE -- decodes cleanly as 2021-01-27).
    Two-digit year is inherently ambiguous for very old deeds, but this
    is current MOD-IV tax-assessment data, not a historical archive --
    treating YY as 20YY, not attempting a 19xx/20xx split-century
    heuristic since no pre-2000 sample has been seen to calibrate one."""
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) != 6 or not s.isdigit():
        return None  # unexpected shape -- don't guess
    yy, mm, dd = s[0:2], s[2:4], s[4:6]
    return f"20{yy}-{mm}-{dd}"


# CONFIRMED official NJ Division of Taxation MOD-IV property classification
# codes -- a published state standard, not inferred from ambiguous sample
# data (unlike the LAND_DESC mistake this replaces, below). PROP_CLASS is
# the real property-type field; LAND_DESC was wrongly assumed to be one in
# the first version of this spider (real sample value '103X 165' turned
# out to be lot frontage x depth, not a use description).
NJ_PROP_CLASS_DESC = {
    "1": "Vacant Land",
    "2": "Residential",
    "3A": "Farm Regular",
    "3B": "Farm Qualified",
    "4A": "Commercial",
    "4B": "Industrial",
    "4C": "Apartment",
    "5A": "Railroad Class I",
    "5B": "Railroad Class II",
    "6A": "Public Utility",
    "6B": "Public Utility Personal Property",
    "15A": "Public School Property",
    "15B": "Other School Property",
    "15C": "Public Property",
    "15D": "Church and Charitable Property",
    "15E": "Cemeteries and Graveyards",
    "15F": "Other Exempt",
}


def _geojson_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """Same rough-centroid approach as ct_spider.py/md_spider.py."""
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


class NJSpider(StateSpider):
    state_code = "NJ"

    def __init__(self):
        self._schema_diagnostic_printed = False

    def list_towns(self) -> list[str]:
        """Query the service's own distinct MUN_NAME values, same
        rationale as CTSpider/MASpider.list_towns() -- MUN_NAME is a
        confirmed real human-readable field, unlike MD's JURSCODE."""
        params = {
            "where": "1=1",
            "outFields": "MUN_NAME",
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
        towns = sorted({f["attributes"]["MUN_NAME"] for f in data.get("features", [])
                         if f["attributes"].get("MUN_NAME")})
        return towns

    def _query_page(self, town: str, offset: int) -> dict:
        safe_town = town.replace("'", "''")
        where = f"UPPER(MUN_NAME) = UPPER('{safe_town}')"
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "geojson",  # confirmed supported ("Supported Query Formats: JSON, geoJSON, PBF")
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
        attrs = feature.get("properties", {})
        geometry = feature.get("geometry")
        lat, lon = _geojson_centroid(geometry)

        # PIN_NODUP CONFIRMED live 2026-08-24: a real field, present on
        # every sample record, whose name itself signals NJOGIS already
        # handles the exact duplicate-PIN problem CT/MD needed a custom
        # _dedupe_property_ids() for -- using it directly instead of
        # PAMS_PIN means this spider likely doesn't need that ported
        # fix at all. Falls back to PAMS_PIN only if PIN_NODUP is ever
        # null on some record (not seen yet, but not to be assumed).
        pin = attrs.get("PIN_NODUP") or attrs.get("PAMS_PIN")
        record = {
            "property_id": f"NJ:{pin}" if pin else None,
            "state": "NJ",
            "county": attrs.get("COUNTY"),
            "municipality": attrs.get("MUN_NAME") or town,
            "parcel_id": pin,
            "address": attrs.get("PROP_LOC") or attrs.get("ST_ADDRESS"),
            "city": attrs.get("MUN_NAME") or town,
            "zip": attrs.get("ZIP5") or attrs.get("ZIP_CODE"),
            "latitude": lat,
            "longitude": lon,
            "acreage": _num(attrs, "CALC_ACRE"),
            "assessed_value": _num(attrs, "NET_VALUE"),
            "assessed_land_value": _num(attrs, "LAND_VAL"),
            "assessed_building_value": _num(attrs, "IMPRVT_VAL"),
            "assessment_year": None,  # no confirmed assessment-year field on this layer
            "last_sale_price": _num(attrs, "SALE_PRICE"),
            "last_sale_date": _nj_date_to_iso(attrs.get("DEED_DATE")),  # confirmed YYMMDD format, see docstring
            "building_sqft": None,  # not confirmed on this layer -- see docstring
            "bedrooms": None,  # not confirmed -- see docstring
            "bathrooms": None,  # not confirmed -- see docstring
            "year_built": _num(attrs, "YR_CONSTR"),
            "property_type": standardize_property_type(
                NJ_PROP_CLASS_DESC.get(attrs.get("PROP_CLASS"), attrs.get("PROP_CLASS"))
            ),  # PROP_CLASS decoded via the official NJ standard table, not LAND_DESC (see docstring -- LAND_DESC is lot dimensions, not a use description)
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
                real_keys = sorted(features[0]["properties"].keys())
                print(f"  [schema check] NJ field names: {real_keys}")
                print(f"  [schema check] sample record: {features[0]['properties']}")
                self._schema_diagnostic_printed = True
            for feature in features:
                records.append(self._normalize_feature(feature, town))
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="*", help="NJ municipality names, e.g. Newark")
    parser.add_argument("--list-towns", action="store_true",
                         help="print all distinct MUN_NAME values found live, then exit")
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    spider = NJSpider()

    if args.list_towns:
        towns = spider.list_towns()
        print(f"{len(towns)} distinct MUN_NAME values found:")
        for t in towns:
            print(f"  {t}")
        return

    if not args.towns:
        print("ERROR: pass one or more municipality names, or use --list-towns first")
        sys.exit(1)

    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()