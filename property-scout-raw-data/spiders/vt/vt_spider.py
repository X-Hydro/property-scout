"""
Vermont spider — Property Values Database

Source: VCGI (Vermont Center for Geographic Information) statewide
standardized parcel layer, joined to the VT Department of Taxes annual
Grand List. Same shape as MD/NJ/MA: one state agency, one queryable
layer, all 247 municipalities, geometry pre-joined to assessed-value
attributes -- no per-town scraping. Confirmed all-247-towns coverage
per VCGI's own program description (three-year 2017-2019 state-funded
mapping effort brought every town in).

    https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/
    FS_VCGI_VTPARCELS_WM_NOCACHE_v2/FeatureServer/1/query

Layer 1 (Cadastral_VTPARCELS_poly) is ACTIVE parcels -- this is the one
this spider queries. Layer 0 on the same FeatureServer holds historical/
inactive records and is deliberately not used here.

CONFIRMED FIELD NAMES AND TYPES -- fetched directly from the layer's own
live ?f=json metadata (2026-08-26), not from documentation prose, same
discipline as every other state spider in this project.

  SPAN, GLIST_SPAN, MAPID, PARCID, PROPTYPE, YEAR, GLYEAR, TOWN, TNAME,
  SOURCENAME, SOURCETYPE, SOURCEDATE, EDITMETHOD, EDITOR, EDITDATE,
  MATCHSTAT, EDITNOTE, OWNER1, OWNER2, ADDRGL1, ADDRGL2, CITYGL, STGL,
  ZIPGL, DESCPROP, LOCAPROP, CAT, RESCODE, ACRESGL, REAL_FLV, HSTED_FLV,
  NRES_FLV, LAND_LV, IMPRV_LV, EQUIPVAL, EQUIPCODE, INVENVAL, HSDECL,
  HSITEVAL, VETEXAMT, EXPDESC, ENDDATE, STATUTE, EXAMT_HS, EXAMT_NR,
  UVREDUC_HS, UVREDUC_NR, GLVAL_HS, GLVAL_NR, CRHOUSPCT, MUNGL1PCT,
  AOEGL_HS, AOEGL_NR, E911ADDR

CONFIRMED VIA A REAL BARNET RUN (2026-08-26) -- corrected from the
original guess:

  - PROPTYPE is NOT a land-use/property-type field. Real data shows it
    takes exactly 4 values statewide-shaped: 'PARCEL' (the taxable/
    real-estate features), 'WATER', 'ROW_ROAD', 'ROW_RAIL' (non-taxable
    geometry sharing the same layer). It's a GEOMETRY FEATURE
    classifier, not a use classifier -- the original version of this
    spider used `PROPTYPE or CAT`, which meant CAT was NEVER read,
    since PROPTYPE is truthy on ~97% of rows. Fixed below: PROPTYPE is
    now used only to separate taxable parcels from non-taxable
    geometry; CAT (Category, Real Estate only -- confirmed real value
    'M' in the sample) is the actual land-use source for taxable
    parcels, standardized via standardize_property_type() same as
    every other state. VT's CAT code table (VT Grand List uses codes
    like R1/R2/C1/C2/I1/F/U/O/M/MHU/MHL) is NOT yet fully confirmed --
    only 'M' has been seen live -- so most CAT values will still pass
    through unmapped until more are observed and added to the shared
    CSV, same position CT/MD were in at first.
  - MATCHSTAT is a confirmed real field ('MATCH' in the sample) whose
    documented job is flagging whether VCGI successfully joined that
    parcel's geometry to its Grand List record. This is the actual
    diagnostic for null assessed_value on an otherwise-real parcel
    (has a SPAN) -- rather than silently emitting a null, fetch_town()
    now counts and reports non-'MATCH' rows separately from ordinary
    missing-parcel_id rows, so a null assessed_value can be told apart
    as "VCGI's own join failed upstream" vs. "something else is wrong."
  - SOURCEDATE/EDITDATE are confirmed 8-char strings but the actual
    format (YYYYMMDD vs something else) is unconfirmed -- not mapped to
    any output field here, so this doesn't block anything, but don't
    assume YYYYMMDD if you go looking at them later.

STRUCTURAL GAP, confirmed absent from this layer's real field list --
this is the biggest difference from every other state spider in this
project: NO sale history and NO building characteristics fields exist
on this layer at all. No SALE_PRICE/DEED_DATE equivalent, no
YEAR_BUILT/BLD_AREA/bedrooms/bathrooms equivalent. VT's Grand List is a
valuation-and-ownership record, not a CAMA record with property
characteristics -- last_sale_price, last_sale_date, building_sqft,
bedrooms, bathrooms, and year_built are therefore always None from this
source, not a mapping oversight. If VT ever needs those fields, it's
worth checking whether individual towns' own vendor systems (NEMRC,
Vision Government Solutions, etc.) publish separate CAMA extracts --
out of scope for this first pass, same as MD's noted-but-unexplored
MD_ComputerAssistedMassAppraisal service.

OTHER CONFIRMED GAPS:
  - No county field at all -- confirmed absent, and this matches
    Vermont's actual government structure (no county assessors, no
    county GIS offices; property records are entirely town-level), not
    a coverage hole like NY's county-by-county polygon consent problem.
    county is always None, same as MA's county=None.
  - ZIPGL is the OWNER's mailing zip (alias "Mailing Address Zip"), not
    the property's zip code -- there is no property-address zip field
    on this layer at all. Do NOT populate the `zip` output field from
    ZIPGL; it would silently mix owner mailing data into a
    property-location field. zip is always None here.
  - No dedicated property street-address field (no ST_ADDRESS/
    PROP_LOC-equivalent). E911ADDR ("Emergency 911 Address") is the
    best available real-property address field and is used as the
    primary source; LOCAPROP ("Location") and DESCPROP ("Property
    Description") are lower-confidence fallbacks, in that order.
  - parcel_id/property_id use SPAN ("GIS SPAN") -- confirmed as VT's
    actual statewide unique parcel key (School Property Account
    Number), the field VCGI itself uses to join municipal geometry to
    the Grand List. GLIST_SPAN ("Grand List SPAN") is a secondary
    fallback if SPAN is ever null on a record -- not yet observed, not
    assumed.
  - TOWN (len 30) vs TNAME ("Grand-List Town-Name", len 100): TOWN is
    used below for filtering/display since it's the shorter canonical-
    looking field, matching the CT Town_Name / MA CITY pattern -- but
    which of the two is authoritative hasn't been confirmed against a
    real record. Worth checking on the first real run whether they
    ever disagree for the same parcel.

Usage:
    python -m spiders.vt.vt_spider --list-towns
    python -m spiders.vt.vt_spider Barnet --out data/
"""

import sys
import csv
import json
import argparse
from datetime import date
from functools import lru_cache
from pathlib import Path
import urllib.request
import urllib.parse

from ..common.base import StateSpider, SpiderError

BASE_QUERY_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_VTPARCELS_WM_NOCACHE_v2/FeatureServer/1/query"
)
PAGE_SIZE = 2000  # MaxRecordCount confirmed from live layer metadata
SOURCE_TAG = "VT_VCGI_StatewideParcels_GrandList"


def _num(attrs: dict, field: str):
    v = attrs.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _geojson_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """Same rough-centroid approach as md_spider.py/nj_spider.py."""
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


# CONFIRMED official VT Dept. of Taxes Grand List category codes (source:
# tax.vermont.gov Property Category Codes / PVR Annual Report) -- decoded
# to full descriptions LOCALLY, same precedent as NJ's NJ_PROP_CLASS_DESC,
# rather than feeding short opaque codes directly into any standardizer.
# Decoding always happens (a raw 'C' or 'F' should never leak downstream
# unreadable), but which of these decoded descriptions get bucketed into
# a shared/reused property_type category is a separate, deliberate,
# narrower decision -- see VT_PROPERTY_TYPE_MAPPING_CSV below.
#
# 'C' is UNCONFIRMED against the official code -- the department's own
# glossary lists the commercial code as 'COMM', but Barnet's live data
# returned 'C'. Decoded to "Commercial" regardless (meaning is clear
# either way), but flagged in case a future town's data reveals 'C' and
# 'COMM' are actually two different real codes, not the same one
# spelled two ways.
#
# 'M' (Miscellaneous) is intentionally left undecoded to anything more
# specific -- checked against Barnet's real data and it does NOT
# correlate with the 29 real-parcel/null-assessed_value records (those
# all have CAT itself null, not 'M') so there's no data-quality reason
# to force a guess here.
#
# 'O' (Other -- used for condos/lakefront per the department's own
# description) has not been observed in any real data yet (absent from
# Barnet's full distinct-value list) -- included here since it's an
# official code, but unconfirmed live.
VT_CAT_DESC = {
    "R1": "Residential with fewer than 6 acres",
    "R2": "Residential with 6 or more acres",
    "MHU": "Mobile home un-landed",
    "MHL": "Mobile home landed",
    "S1": "Seasonal home with fewer than 6 acres",
    "S2": "Seasonal home with 6 or more acres",
    "C": "Commercial",  # unconfirmed vs. official 'COMM' -- see note above
    "F": "Farm",
    "UE": "Utility Electric",
    "UO": "Utility Other",
    "W": "Woodland",
    "O": "Other",  # not yet observed live -- see note above
    # "M" deliberately absent -- passed through as raw "M"
}

VT_PROPERTY_TYPE_MAPPING_CSV = Path(__file__).parent / "property_type_mapping.csv"


@lru_cache(maxsize=1)
def _load_vt_property_type_mapping() -> dict[str, str]:
    """VT-LOCAL property type standardization, deliberately kept separate
    from spiders/common/property_types.py's shared cross-state CSV.

    Two reasons this lives here instead of in the shared file, both
    explicit product decisions rather than a shortcut:

    1. VT's Grand List codes are short and state-specific (R1, C, F, W,
       M...). The shared CSV does an exact, unscoped, case-insensitive
       match across every state's raw values in one flat table -- a
       bare single-letter/short alias risks a silent cross-state
       collision with some future state's own short code for something
       unrelated, in a way the shared CSV's same-key-different-value
       check can't catch (it only guards the same literal string
       mapping to two different standardized names, not two different
       states meaning two different things by similarly short strings).
       VT_CAT_DESC above still decodes every short code to a full
       description first, so this file's own keys are readable English,
       not codes -- but keeping the file itself local avoids ever
       writing VT-only vocabulary into a file every other state's
       spider also reads from.
    2. Only "Single Family" and "Vacant Land" (both already
       cross-state, already used by CT/MD/NJ/MA) are reused here.
       "Mobile Home", "Seasonal/Vacation Residential", "Commercial",
       "Farm", "Utility", and "Other" -- decoded, readable, but NOT
       bucketed into anything -- deliberately are NOT added as new
       standardized categories to the shared vocabulary. If a later
       state's data makes one of those a real cross-state category, add
       it to the shared CSV then, with that state's own confirmed real
       values alongside it -- not preemptively from VT alone.

    Same matching convention as the shared loader: case-insensitive,
    outer-trimmed, exact match only, unrecognized values returned
    unchanged (not guessed at, not blanked).
    """
    mapping: dict[str, str] = {}
    with open(VT_PROPERTY_TYPE_MAPPING_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            standardized = (row.get("standardized") or "").strip()
            raw_aliases = (row.get("raw_aliases") or "").strip()
            if not standardized:
                continue
            for raw in raw_aliases.split("|"):
                raw = raw.strip()
                if raw:
                    mapping[raw.lower()] = standardized
    return mapping


def _standardize_vt_property_type(decoded_value: str | None) -> str | None:
    if decoded_value is None:
        return None
    trimmed = decoded_value.strip()
    if not trimmed:
        return None
    return _load_vt_property_type_mapping().get(trimmed.lower(), trimmed)


# Confirmed real PROPTYPE values (see module docstring) -- these are
# non-taxable geometry sharing the parcel layer, not real estate. Kept
# as their own property_type labels (not run through
# standardize_property_type, which is for real-estate use categories)
# so downstream comps/gap-analysis code can filter them out explicitly
# instead of them showing up disguised as an ordinary "unmapped" type.
NON_TAXABLE_PROPTYPES = {"WATER", "ROW_ROAD", "ROW_RAIL"}


def _build_address(attrs: dict) -> str | None:
    """No dedicated property-address field exists on this layer -- see
    module docstring. E911ADDR is the best real-property address field
    available; LOCAPROP and DESCPROP are lower-confidence fallbacks."""
    for field in ("E911ADDR", "LOCAPROP", "DESCPROP"):
        v = attrs.get(field)
        if v and str(v).strip():
            return str(v).strip()
    return None


class VTSpider(StateSpider):
    state_code = "VT"

    def __init__(self):
        self._schema_diagnostic_printed = False

    def list_towns(self) -> list[str]:
        """Same rationale as CTSpider/MASpider/NJSpider.list_towns() --
        query the service's own distinct TOWN values instead of
        hardcoding all 247 Vermont municipalities."""
        params = {
            "where": "1=1",
            "outFields": "TOWN",
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
        towns = sorted({f["attributes"]["TOWN"] for f in data.get("features", [])
                         if f["attributes"].get("TOWN")})
        return towns

    def _query_page(self, town: str, offset: int) -> dict:
        safe_town = town.replace("'", "''")
        where = f"UPPER(TOWN) = UPPER('{safe_town}')"
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

        span = attrs.get("SPAN") or attrs.get("GLIST_SPAN")
        proptype = attrs.get("PROPTYPE")

        # PROPTYPE separates taxable parcels from non-taxable geometry
        # (water bodies, rights-of-way) sharing this layer -- CAT is the
        # actual land-use field for real parcels. See module docstring;
        # this was reversed in the first version of this spider.
        if proptype in NON_TAXABLE_PROPTYPES:
            property_type = proptype  # not run through standardize_property_type -- not a real-estate use category
        else:
            cat = attrs.get("CAT")
            # Decode VT's short official code to a full description first
            # (see VT_CAT_DESC above), then run through the VT-LOCAL
            # mapping only -- NOT the shared cross-state standardizer.
            # See _load_vt_property_type_mapping()'s docstring for why.
            property_type = _standardize_vt_property_type(VT_CAT_DESC.get(cat, cat))

        record = {
            "property_id": f"VT:{span}" if span else None,
            "state": "VT",
            "county": None,  # confirmed absent from this layer -- VT has no county assessors, see docstring
            "municipality": attrs.get("TOWN") or town,
            "parcel_id": span,
            "address": _build_address(attrs),
            "city": attrs.get("TOWN") or town,
            "zip": None,  # ZIPGL is the OWNER's mailing zip, not property zip -- do not use, see docstring
            "latitude": lat,
            "longitude": lon,
            "acreage": _num(attrs, "ACRESGL"),
            "assessed_value": _num(attrs, "REAL_FLV"),
            "assessed_land_value": _num(attrs, "LAND_LV"),
            "assessed_building_value": _num(attrs, "IMPRV_LV"),
            "assessment_year": attrs.get("GLYEAR"),
            "last_sale_price": None,  # not on this layer -- see docstring (structural gap)
            "last_sale_date": None,  # not on this layer -- see docstring (structural gap)
            "building_sqft": None,  # not on this layer -- see docstring (structural gap)
            "bedrooms": None,  # not on this layer -- see docstring (structural gap)
            "bathrooms": None,  # not on this layer -- see docstring (structural gap)
            "year_built": None,  # not on this layer -- see docstring (structural gap)
            "property_type": property_type,
            "source": SOURCE_TAG,
            "source_url": BASE_QUERY_URL,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        return record

    def fetch_town(self, town: str) -> list[dict]:
        records = []
        offset = 0
        unmatched_with_span = 0  # real parcel (has SPAN) but VCGI's own MATCHSTAT says the Grand List join failed
        while True:
            page = self._query_page(town, offset)
            features = page.get("features", [])
            if not self._schema_diagnostic_printed and features:
                real_keys = sorted(features[0]["properties"].keys())
                print(f"  [schema check] VT field names: {real_keys}")
                print(f"  [schema check] sample record: {features[0]['properties']}")
                self._schema_diagnostic_printed = True
            for feature in features:
                attrs = feature.get("properties", {})
                record = self._normalize_feature(feature, town)
                # Distinguish "VCGI's own geometry-to-Grand-List join
                # failed" (nothing this spider can fix) from any other
                # cause of a null assessed_value on a real parcel, using
                # the confirmed MATCHSTAT field -- see module docstring.
                if (record["parcel_id"] and record["assessed_value"] is None
                        and attrs.get("MATCHSTAT") != "MATCH"):
                    unmatched_with_span += 1
                records.append(record)
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        if unmatched_with_span:
            print(f"  NOTE: {unmatched_with_span} parcel(s) have a real parcel_id but no "
                  f"assessed_value, confirmed via MATCHSTAT as a VCGI-side Grand List join "
                  f"failure (not a mapping bug in this spider)")
        return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="*", help="VT municipality names, e.g. Barnet")
    parser.add_argument("--list-towns", action="store_true",
                         help="print all distinct TOWN values found live, then exit")
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    spider = VTSpider()

    if args.list_towns:
        towns = spider.list_towns()
        print(f"{len(towns)} distinct TOWN values found:")
        for t in towns:
            print(f"  {t}")
        return

    if not args.towns:
        print("ERROR: pass one or more town names, or use --list-towns first")
        sys.exit(1)

    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()