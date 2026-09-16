"""
Rhode Island spider — Property Values Database

TWO value sources, selected via value_source (mirrors nh_spider.py's
vgsi/offmarket split, adapted -- RI has no assessor-scraping equivalent
to VGSI at all, so there is no "assessor" option here, just geometry
alone vs. geometry+offmarket):

  - "dem" (default): RIDEM Tax_Parcels geometry only. No assessed
    values -- see the HARD LIMITATION note below, unchanged from the
    original version of this file.

  - "offmarket": RIDEM parcel geometry + RealtyAPI/Zillow off-market
    values, joined via point-in-polygon, reusing the SAME state-agnostic
    offmarket_value_sweep.py / join_parcels_offmarket.py NH already
    uses -- no RI-specific sweep/join logic needed, only RI-specific
    parcel fetch + listing-matching.

    REAL DIFFERENCE FROM NH, but NOT the one originally assumed here:
    NHSpider matches listings to a town by exact string equality
    against a REAL town name ("Lincoln"), since NH's whole pipeline is
    already name-keyed. RI's only per-town identifier from RIDEM is a
    2-character TownCode ("BA"), which never equals a RealtyAPI
    address.city value ("Barrington") -- so copying NH's name-match
    approach verbatim would need a TownCode -> name table.

    CORRECTED: that table isn't actually necessary. By the time the
    offmarket sweep runs for a TownCode, this spider has ALREADY
    fetched that town's real parcel geometry (fetch_town() calls
    _get_raw_parcels_geojson() first, unconditionally). So listings are
    scoped to a town by a SPATIAL bounding-box check against that
    town's own just-downloaded parcels, in
    _load_town_listing_points() -- no name, no code table, no extra
    dependency. A bounding box is looser than the true town outline
    (it can admit a listing just across a neighbor's straight-line
    border), but that's an acceptable looseness for SEEDING a sweep
    grid -- the actual value join afterward is a real point-in-polygon
    match against parcels via join_parcels_offmarket.py, unaffected by
    this approximation. Worst case is a marginally wider seed area,
    not a wrong join.

RESTRUCTURED from the original DEM-only version of this file: the raw
paginated ArcGIS fetch now writes a RAW (un-normalized, original RIDEM
attribute names) geojson to disk via _get_raw_parcels_geojson(), same
role as NH's _get_granit_geojson(). Normalization into
COMMON_SCHEMA_FIELDS happens in a separate _normalize_feature() pass
AFTER the optional offmarket join, so join_parcels_offmarket.py (which
expects to read/write plain feature properties, not
already-schema-mapped records) can operate on it exactly the way it
already does for NH's GRANIT files.

    https://risegis.ri.gov/hosting/rest/services/RIDEM/Tax_Parcels/MapServer/0/query

CONFIRMED LIVE (via the layer's own /0?f=json metadata endpoint, checked
2026-09-14): MaxRecordCount 2000 (pagination required, same as CT/MA/NH).
Supported Query Formats: JSON, geoJSON, PBF. Copyright Text: "RI State,
37 Towns" -- but list_towns() returned 39 distinct codes live, matching
RI's real municipality count; the copyright text is stale, not the data.

Confirmed full field list, from the layer schema itself, not prose docs:
    OBJECTID, PlatLot, Acres, E911, TownCode, PctImp, PctDev, PctOS,
    E911_Type, IMP_sqft, Last_UPD, E911Desc, Shape, OverlaysDWSupply,
    PWS_Wshed, GW_Acquifer, GW_Recharge, CWHPA,
    EPA_Sole_Source_Acquifer, PctForest

HARD LIMITATION, confirmed by the absence of any value-bearing field
above: this is DEM's parcel/environmental-overlay layer, NOT an
assessor CAMA layer, despite the "Tax Parcels" service name. There is
no assessed value, land value, building value, sale price/date,
bedrooms, bathrooms, year built field anywhere in this layer -- on the
"dem" value_source, every value field in COMMON_SCHEMA_FIELDS stays
None. The "offmarket" path is the only way this spider produces real
assessed_value/tax_assessed_value data.

CONFIRMED from a real returned record (2026-09-14, TownCode='BA'):
  - E911Desc is a land-use/property-type description (real sample
    value: "Single Family Home"), mapped into `property_type` via the
    shared standardize_property_type(). Only ONE real value seen so
    far -- watch the [schema check] output across more towns and add
    aliases to spiders/common/property_types.py as new values appear.
  - E911 ("Y") and E911_Type ("R1") remain unconfirmed -- carried
    through raw as ri_e911 / ri_e911_type, not mapped to any schema
    field.
  - address is confirmed absent from this layer entirely.

UNCONFIRMED:
  - TownCode -> real town name: see TOWN_CODE_TO_NAME below, currently
    empty pending build_ri_town_code_map.py's output.
  - Last_UPD's date format (string field, length 12) -- carried
    through raw as ri_last_updated, not parsed.

property_id built as "RI:{TownCode}:{PlatLot}" -- PlatLot only
confirmed unique WITHIN a town, not statewide.

Usage:
    python -m spiders.ri.ri_spider BA --out data/                    # DEM geometry only
    python -m spiders.ri.ri_spider BA --value-source offmarket --out data/   # + offmarket join, requires
                                                                              # TOWN_CODE_TO_NAME filled in
                                                                              # and REALTYAPI_KEY set
    python -m spiders.ri.ri_spider --list-towns                       # see real TownCode values
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import date
import urllib.request
import urllib.parse

from ..common.base import StateSpider, SpiderError
from ..common.property_types import standardize_property_type

PIPELINE_DIR = Path(__file__).parent          # spiders/ri/
PROJECT_ROOT = PIPELINE_DIR.parent.parent      # project root -- state-agnostic offmarket pieces live here,
                                                # same as nh_spider.py's PROJECT_ROOT
sys.path.insert(0, str(PROJECT_ROOT))

_REQUIRED_ROOT_FILES = ["offmarket_value_sweep.py", "join_parcels_offmarket.py", "offmarket_to_geojson.py"]
_missing_root_files = [f for f in _REQUIRED_ROOT_FILES if not (PROJECT_ROOT / f).exists()]
if _missing_root_files:
    raise SpiderError(
        f"Expected these state-agnostic files directly under PROJECT_ROOT ({PROJECT_ROOT}), "
        f"but they're missing: {_missing_root_files}. (PIPELINE_DIR resolved to {PIPELINE_DIR} "
        f"-- if PROJECT_ROOT looks wrong, spiders/ri/ may not be exactly two levels under your "
        f"actual project root.)"
    )

try:
    import offmarket_value_sweep         # state-agnostic (project root) -- same module NH uses
    import join_parcels_offmarket        # state-agnostic (project root) -- point-in-polygon join
    import offmarket_to_geojson          # state-agnostic (project root) -- QGIS-loadable debug points
except ImportError as e:
    offmarket_value_sweep = None
    join_parcels_offmarket = None
    offmarket_to_geojson = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

BASE_QUERY_URL = "https://risegis.ri.gov/hosting/rest/services/RIDEM/Tax_Parcels/MapServer/0/query"
PAGE_SIZE = 2000  # matches confirmed layer MaxRecordCount
SOURCE_TAG_DEM = "RI_DEM_TaxParcels_Live"
SOURCE_TAG_OFFMARKET = "RI_DEM_OFFMARKET"
OFFMARKET_RAW_DIR = "offmarket-raw"  # per-town subdirectory, same role as NH's OFFMARKET_RAW_DIR
SEED_RADIUS_MILES = 2.0              # same starting cell size NH's seed_grid() uses

# OPTIONAL cosmetic mapping, NOT required for the offmarket path to
# work (see module docstring -- listings scoping is spatial now, via
# each TownCode's own parcel bounding box, not this table). If filled
# in later (e.g. from build_ri_town_code_map.py's output, reviewed
# against a real map), it's used only to populate `municipality`/`city`
# with a real name instead of the raw code. Left empty by default.
TOWN_CODE_TO_NAME: dict[str, str] = {
    # "BA": "Barrington",
    # ...
}

# Extra (non-COMMON_SCHEMA_FIELDS) attributes carried through verbatim
# from the raw RIDEM record, prefixed ri_ since they're extensions, not
# part of the shared cross-state schema.
_PASSTHROUGH_FIELDS = {
    "PctImp": "ri_pct_impervious",
    "PctDev": "ri_pct_developed",
    "PctOS": "ri_pct_open_space",
    "PctForest": "ri_pct_forest",
    "IMP_sqft": "ri_impervious_sqft",
    "E911": "ri_e911",
    "E911_Type": "ri_e911_type",
    "Last_UPD": "ri_last_updated",
    "OverlaysDWSupply": "ri_overlay_dw_supply",
    "PWS_Wshed": "ri_overlay_pws_watershed",
    "GW_Acquifer": "ri_overlay_gw_aquifer",
    "GW_Recharge": "ri_overlay_gw_recharge",
    "CWHPA": "ri_overlay_cwhpa",
    "EPA_Sole_Source_Acquifer": "ri_overlay_epa_sole_source_aquifer",
}


def _num(attrs: dict, field: str):
    v = attrs.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _strip_z(geometry: dict | None) -> dict | None:
    """Drops any Z coordinate from Polygon/MultiPolygon ring vertices.

    CONFIRMED NEEDED (2026-09): RIDEM's ArcGIS layer returns 3-tuple
    vertices ([lon, lat, z], not [lon, lat]) even though these are flat
    parcel boundaries with no meaningful elevation data -- load_property_values.py
    failed with psycopg2.errors.InvalidParameterValue: "Geometry has Z
    dimension but column does not". shapely tolerates 3D geometry fine
    (join_parcels_offmarket.py's .contains()/.distance() calls never
    errored on it), so this went undetected until the final PostGIS
    load step. Applied here, at fetch time, so every downstream
    consumer (the raw parcels file used for both the offmarket join
    AND bbox-based listing scoping, plus the final normalized output)
    only ever sees 2D coordinates -- stripping later (e.g. only in
    _write_geojson) would still leave 3D geometry in the raw file that
    _load_town_listing_points() and join_town_file() both read."""
    if not geometry:
        return geometry
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return geometry

    def strip_ring(ring):
        return [pt[:2] for pt in ring]

    if gtype == "Polygon":
        geometry["coordinates"] = [strip_ring(ring) for ring in coords]
    elif gtype == "MultiPolygon":
        geometry["coordinates"] = [[strip_ring(ring) for ring in poly] for poly in coords]
    # other geometry types left untouched -- this layer is confirmed polygon-only
    return geometry


def _geojson_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """Average-of-vertices centroid (not a true area-weighted centroid,
    same simplification MA's _esri_ring_centroid() / NH's
    _ring_centroid_from_geojson() use), outer ring(s) only. Handles
    Polygon and MultiPolygon. Returns (lat, lon)."""
    if not geometry:
        return None, None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None, None
    if gtype == "Polygon":
        rings = [coords[0]]
    elif gtype == "MultiPolygon":
        rings = [poly[0] for poly in coords]
    else:
        return None, None
    xs, ys = [], []
    for ring in rings:
        for pt in ring:
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return None, None
    return sum(ys) / len(ys), sum(xs) / len(xs)  # (lat, lon)


def _geojson_bbox(geometry: dict | None) -> tuple[float, float, float, float] | None:
    """(min_lat, max_lat, min_lon, max_lon) from every vertex, Polygon
    or MultiPolygon, ALL rings (not just outer -- a hole doesn't shrink
    the bbox, but including it costs nothing and avoids a subtle bug if
    an inner ring's vertices ever extended past an outer ring's, e.g.
    self-intersecting/malformed source geometry)."""
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if gtype == "Polygon":
        rings = coords
    elif gtype == "MultiPolygon":
        rings = [ring for poly in coords for ring in poly]
    else:
        return None
    xs, ys = [], []
    for ring in rings:
        for pt in ring:
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return None
    return min(ys), max(ys), min(xs), max(xs)


class RISpider(StateSpider):
    state_code = "RI"

    def __init__(self, out_dir: str = "data", value_source: str = "dem",
                 min_radius: float = 0.25, max_calls: int = 200,
                 listings_file: str = None):
        if value_source not in ("dem", "offmarket"):
            raise SpiderError(f"value_source must be 'dem' or 'offmarket', got {value_source!r}")

        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.value_source = value_source
        self.min_radius = min_radius
        self.max_calls = max_calls
        self.total_api_calls = 0  # cumulative across every fetch_town() call this instance makes,
                                   # same role as NHSpider.total_api_calls -- lets sweep_ri_statewide.py
                                   # track real session cost
        self._seen_e911_desc = set()

        if value_source == "offmarket":
            if _IMPORT_ERROR is not None:
                raise SpiderError(
                    f"Could not import the state-agnostic offmarket pipeline scripts from "
                    f"{PROJECT_ROOT} -- original error: {_IMPORT_ERROR}"
                )
            if not os.environ.get("REALTYAPI_KEY"):
                raise SpiderError("value_source='offmarket' requires REALTYAPI_KEY set in the environment")
            self.listings_file = listings_file or str(PROJECT_ROOT / "realtyapi-data" / "realtyapi_ri.json")
            if not os.path.exists(self.listings_file):
                raise SpiderError(
                    f"value_source='offmarket' requires a statewide listings file at "
                    f"{self.listings_file} (or pass listings_file= explicitly) -- run "
                    f"`python realtyapi_bypolygon_state.py RI --property-type single_family,land` "
                    f"first to generate it. NOT CONFIRMED: this assumes realtyapi_bypolygon_state.py "
                    f"names its output realtyapi_<state_lower>.json, by analogy with NH's "
                    f"realtyapi_nh.json -- verify the real output filename after running it once."
                )

    def list_towns(self) -> list[str]:
        """Returns real, live TownCode values (e.g. 'BA') -- NOT town
        names. See TOWN_CODE_TO_NAME for the name mapping."""
        params = {
            "where": "1=1",
            "outFields": "TownCode",
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
        codes = sorted({f["attributes"]["TownCode"] for f in data.get("features", [])
                         if f["attributes"].get("TownCode")})
        return codes

    def _query_page(self, town_code: str, offset: int) -> dict:
        safe_code = town_code.replace("'", "''")
        params = {
            "where": f"TownCode = '{safe_code}'",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "geojson",
        }
        url = f"{BASE_QUERY_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PropertyValuesDB research tool"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
        data = json.loads(body)
        if isinstance(data, dict) and "error" in data:
            raise SpiderError(f"ArcGIS query error at offset={offset}: {data['error']}")
        return data

    def _get_raw_parcels_geojson(self, town_code: str) -> str:
        """Paginated fetch, written to disk RAW (original RIDEM
        attribute names, un-normalized) -- same role as NH's
        _get_granit_geojson(). Normalization happens later in
        _normalize_feature(), after any offmarket join, so
        join_parcels_offmarket.py can operate on plain properties."""
        out_path = self.out_dir / f"{town_code.lower()}_ri_raw_parcels.geojson"

        if out_path.exists():
            with open(out_path) as f:
                cached = json.load(f)
            n_cached = len(cached.get("features", []))
            print(f"  reusing existing RIDEM fetch at {out_path} ({n_cached} parcel(s), no network "
                  f"call). Delete this file to force a re-fetch.")
            return str(out_path)

        all_features = []
        offset = 0
        new_desc_values = set()
        while True:
            page = self._query_page(town_code, offset)
            features = page.get("features", [])
            for feature in features:
                desc = feature.get("properties", {}).get("E911Desc")
                if desc and desc not in self._seen_e911_desc:
                    new_desc_values.add(desc)
                    self._seen_e911_desc.add(desc)
                feature["geometry"] = _strip_z(feature.get("geometry"))  # see _strip_z() docstring
            all_features.extend(features)
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        if new_desc_values:
            print(f"  [schema check] new E911Desc values in {town_code}: {sorted(new_desc_values)}")
        geojson = {"type": "FeatureCollection", "features": all_features}
        with open(out_path, "w") as f:
            json.dump(geojson, f)
        print(f"  wrote {len(all_features)} RIDEM parcels to {out_path}")
        return str(out_path)

    def _load_town_listing_points(self, raw_parcels_path: str) -> list:
        """Coordinates of every active listing whose (lat, lon) falls
        within this TownCode's own parcel bounding box -- computed from
        the RIDEM parcels ALREADY fetched for this town (fetch_town()
        gets raw_parcels_path before this is ever called). No name or
        code table needed. See module docstring for why a bounding box
        is an acceptable looseness here (seeding a sweep grid, not the
        real value join, which is exact point-in-polygon against
        parcels later)."""
        with open(raw_parcels_path) as f:
            parcels = json.load(f)

        lats, lons = [], []
        for feat in parcels["features"]:
            geom = feat.get("geometry")
            bbox = _geojson_bbox(geom)
            if bbox is None:
                continue
            min_lat, max_lat, min_lon, max_lon = bbox
            lats.extend([min_lat, max_lat])
            lons.extend([min_lon, max_lon])

        if not lats:
            print(f"  [offmarket] WARNING: no usable parcel geometry to derive a bounding box from "
                  f"-- treating as zero listings for this town")
            return []

        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        with open(self.listings_file) as f:
            payload = json.load(f)
        records = payload if isinstance(payload, list) else payload.get("searchResults", [])
        points = []
        for r in records:
            addr = r.get("address") or {}
            lat, lon = addr.get("latitude"), addr.get("longitude")
            if lat is None or lon is None:
                continue
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                points.append((lat, lon))
        return points

    def _write_debug_points(self, town_code: str, raw_path) -> None:
        debug_path = Path(str(raw_path).replace(".json", "_debug.geojson"))
        if debug_path.exists():
            return
        with open(raw_path) as f:
            payload = json.load(f)
        geojson = offmarket_to_geojson.payload_to_geojson(payload)
        with open(debug_path, "w") as f:
            json.dump(geojson, f)
        print(f"  [offmarket] debug point layer ({len(geojson['features'])} point(s)) -> {debug_path}")

    def _sweep_offmarket_for_town(self, town_code: str, raw_parcels_path: str) -> str:
        """Per-TownCode offmarket sweep, structurally identical to
        NHSpider._sweep_offmarket_for_town() -- same caching, same
        budget handling, same seed_grid()/sweep_complete() calls from
        the shared offmarket_value_sweep.py. Only the listing-lookup
        (spatial bbox against raw_parcels_path, see
        _load_town_listing_points) differs from NH's name match."""
        raw_dir = self.out_dir / OFFMARKET_RAW_DIR / town_code.lower()
        raw_path = raw_dir / f"{town_code.lower()}_ri_offmarket_raw.json"

        if raw_path.exists():
            with open(raw_path) as f:
                cached = json.load(f)
            n_cached = len(cached.get("offMarketResults", []))
            print(f"  [offmarket] {town_code}: reusing existing sweep at {raw_path} "
                  f"({n_cached} record(s), 0 new API calls). Delete this file to force a re-sweep.")
            self._write_debug_points(town_code, raw_path)
            return str(raw_dir)

        api_key = os.environ.get("REALTYAPI_KEY")

        listing_points = self._load_town_listing_points(raw_parcels_path)
        seeds = offmarket_value_sweep.seed_grid(listing_points, SEED_RADIUS_MILES)
        print(f"  [offmarket] {len(listing_points)} listing(s) in {town_code}'s parcel bounding box "
              f"-> {len(seeds)} occupied grid cell(s) at {SEED_RADIUS_MILES}mi")

        if not seeds:
            print(f"  [offmarket] {town_code}: no listings, nothing to sweep -- skipping entirely")
            raw_dir.mkdir(parents=True, exist_ok=True)
            with open(raw_path, "w") as f:
                json.dump({"offMarketResults": []}, f)
            return str(raw_dir)

        seen_zpids, out_records, incomplete_areas, call_counter = set(), [], [], [0]
        visited_cells = set()

        for i, (lat, lon) in enumerate(seeds, 1):
            print(f"  [offmarket] cell {i}/{len(seeds)} for {town_code}...")
            if call_counter[0] >= self.max_calls:
                print(f"    SKIPPED -- --max-calls {self.max_calls} budget exhausted for this town")
                incomplete_areas.append((lat, lon, SEED_RADIUS_MILES, "ABORTED-BUDGET"))
                continue
            offmarket_value_sweep.sweep_complete(
                lat, lon, SEED_RADIUS_MILES, self.min_radius, api_key,
                seen_zpids, out_records, incomplete_areas, call_counter,
                self.max_calls, visited_cells
            )

        status = "COMPLETE" if not incomplete_areas else "INCOMPLETE"
        print(f"  [offmarket] {town_code}: {status} -- {call_counter[0]} call(s), "
              f"{len(out_records)} unique record(s)"
              + (f", {len(incomplete_areas)} area(s) not confirmed complete" if incomplete_areas else ""))
        self.total_api_calls += call_counter[0]

        raw_dir.mkdir(parents=True, exist_ok=True)
        with open(raw_path, "w") as f:
            json.dump({"offMarketResults": out_records}, f)
        self._write_debug_points(town_code, raw_path)

        return str(raw_dir)

    def _normalize_feature(self, feature: dict, town_code: str) -> dict:
        attrs = feature.get("properties", {})
        geometry = feature.get("geometry")

        plat_lot = attrs.get("PlatLot")
        lat, lon = _geojson_centroid(geometry)
        # Data-driven, NOT self.value_source -- the join phase in
        # sweep_ri_statewide.py calls this on already-joined features
        # via a spider constructed with value_source="dem" (join is now
        # decoupled from the sweep, see that file's docstring), so the
        # mode flag alone would wrongly report every field as absent.
        # "total_market_value" is present (possibly null) on every
        # feature that went through join_town_file(), whether matched
        # or not -- see fetch_town()'s raw_count==0 fallback, which
        # explicitly setdefaults it too, so this key's PRESENCE (not
        # its value) is the reliable signal.
        is_offmarket = "total_market_value" in attrs

        record = {
            "property_id": f"RI:{town_code}:{plat_lot}" if plat_lot else None,
            "state": "RI",
            "county": None,
            "municipality": TOWN_CODE_TO_NAME.get(town_code) or attrs.get("TownCode") or town_code,
            "parcel_id": plat_lot,
            "address": attrs.get("offmarket_address") if is_offmarket else None,
            "city": TOWN_CODE_TO_NAME.get(town_code) if is_offmarket else None,
            "zip": None,
            "latitude": lat,
            "longitude": lon,
            "acreage": _num(attrs, "Acres"),
            "assessed_value": _num(attrs, "total_market_value") if is_offmarket else None,
            "assessed_land_value": None,
            "assessed_building_value": None,
            "assessment_year": attrs.get("tax_assessment_year") if is_offmarket else None,
            "last_sale_price": None,
            "last_sale_date": None,
            "building_sqft": None,
            "bedrooms": None,
            "bathrooms": None,
            "year_built": None,
            "property_type": standardize_property_type(attrs.get("E911Desc")),
            "source": SOURCE_TAG_OFFMARKET if is_offmarket else SOURCE_TAG_DEM,
            "source_url": BASE_QUERY_URL,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        for src_field, out_key in _PASSTHROUGH_FIELDS.items():
            record[out_key] = attrs.get(src_field)
        if is_offmarket:
            record["ri_offmarket_match_method"] = attrs.get("match_method")
            record["ri_offmarket_match_point_count"] = attrs.get("match_point_count")
            record["ri_offmarket_zpid"] = attrs.get("offmarket_zpid")
        return record

    def fetch_town(self, town: str) -> list[dict]:
        """`town` here is a TownCode (e.g. "BA"), not a name."""
        town_code = town
        raw_path = self._get_raw_parcels_geojson(town_code)

        if self.value_source == "dem":
            with open(raw_path) as f:
                data = json.load(f)
            return [self._normalize_feature(feat, town_code) for feat in data["features"]]

        # offmarket path -- structurally identical to NHSpider.fetch_town()'s offmarket branch
        offmarket_dir = self._sweep_offmarket_for_town(town_code, raw_path)
        joined_path = self.out_dir / f"{town_code.lower()}_ri_offmarket_joined.geojson"

        with open(Path(offmarket_dir) / f"{town_code.lower()}_ri_offmarket_raw.json") as f:
            raw_count = len(json.load(f).get("offMarketResults", []))

        if raw_count == 0:
            print(f"  [offmarket] {town_code}: 0 swept records, passing parcels through with "
                  f"null values (no listings to compare against in this town)")
            with open(raw_path) as f:
                parcels = json.load(f)
            for feature in parcels["features"]:
                for field in ("total_market_value", "offmarket_zpid", "offmarket_address",
                              "tax_assessed_value", "tax_assessment_year", "match_method",
                              "match_point_count"):
                    feature["properties"].setdefault(field, None if field != "match_point_count" else 0)
            with open(joined_path, "w") as f:
                json.dump(parcels, f)
        else:
            points = join_parcels_offmarket.load_offmarket_points(offmarket_dir)
            tree = join_parcels_offmarket.build_index(points)
            print(f"  joining RIDEM parcels + offmarket points for {town_code}...")
            join_parcels_offmarket.join_town_file(raw_path, tree, points, str(joined_path))

        with open(joined_path) as f:
            joined = json.load(f)
        return [self._normalize_feature(feat, town_code) for feat in joined["features"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="*", help="RI TownCode values (alpha, e.g. BA, BI, WO) -- NOT town names, see --list-towns")
    parser.add_argument("--out", default="data")
    parser.add_argument("--value-source", choices=["dem", "offmarket"], default="dem")
    parser.add_argument("--min-radius", type=float, default=0.25, help="offmarket path only")
    parser.add_argument("--max-calls", type=int, default=200, help="offmarket path only -- PER-TOWN budget")
    parser.add_argument("--listings-file", default=None,
                         help="offmarket path only -- path to realtyapi_bypolygon_state.py's output, "
                              "if not at the default realtyapi-data/realtyapi_ri.json")
    parser.add_argument("--list-towns", action="store_true",
                         help="print real TownCode values from a live query and exit, without fetching parcels")
    args = parser.parse_args()

    if args.list_towns:
        # list_towns() doesn't need value_source validation, so build a bare "dem" spider for it
        for code in RISpider(value_source="dem").list_towns():
            print(code)
        return

    if not args.towns:
        parser.error("provide one or more TownCode values, or pass --list-towns")

    spider = RISpider(out_dir=args.out, value_source=args.value_source,
                       min_radius=args.min_radius, max_calls=args.max_calls,
                       listings_file=args.listings_file)
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()