"""
New Hampshire spider — Property Values Database

Value source: GRANIT parcel geometry + RealtyAPI/Zillow off-market
values, joined via point-in-polygon. NH has no single authoritative
statewide assessed-value source, so this routes around per-town VGSI
scraping entirely (removed 2026-09 -- VGSI's per-town scraping was slow
and never reached full statewide coverage; offmarket, though budget-
constrained by RealtyAPI's 2,000 calls/month cap, is at least a single
consistent path with real checkpointed progress).

CONFIRMED (2026-09): a single zipCode-based /search/offmarket call is a
SAMPLE, not a complete set (RealtyAPI support's own words) -- measured
~85% of a real cluster missing on one small NH zip. The reliable approach
is recursive radius-based splitting (pageResultCount hitting 1000 means
the area needs splitting into 4 sub-cells; below 1000 means genuinely
complete for that area), tiled across a town's real parcel extent --
implemented in offmarket_value_sweep.py (project root, state-agnostic,
no NH-specific code in it).

RUNS ENTIRELY PER-TOWN, inside fetch_town(). fetch_town() needs a town's
PARCELS first (to build its seed grid), fetched via _get_granit_geojson()
before anything offmarket-related runs.

BUDGET, CONFIRMED CONSTRAINT (2026-09): RealtyAPI plan is capped at
2,000 calls/month. A rough per-town estimate (56-105 calls seen on
Lebanon/Lincoln, before a caching fix that reduced Lincoln's real cost --
re-test recommended) times ~259 NH towns is WAY over that budget for a
single statewide pass. sweep_nh_statewide.py spreads a full pass across
however many monthly runs it actually takes, via a resumable checkpoint.

Usage:
    python -m spiders.nh_spider Lincoln --out data/
    python -m spiders.nh_spider Lincoln --max-calls 150 --out data/
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import date

from ..common.base import StateSpider, SpiderError

PIPELINE_DIR = Path(__file__).parent          # spiders/nh/ -- NH-SPECIFIC: GRANIT fetch
PROJECT_ROOT = PIPELINE_DIR.parent.parent      # project root -- STATE-AGNOSTIC: offmarket_value_sweep.py,
                                                # join_parcels_offmarket.py, realtyapi_bypolygon_state.py, etc.
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

_REQUIRED_ROOT_FILES = ["offmarket_value_sweep.py", "join_parcels_offmarket.py", "offmarket_to_geojson.py"]
_missing_root_files = [f for f in _REQUIRED_ROOT_FILES if not (PROJECT_ROOT / f).exists()]
if _missing_root_files:
    raise SpiderError(
        f"Expected these state-agnostic files directly under PROJECT_ROOT ({PROJECT_ROOT}), "
        f"but they're missing: {_missing_root_files}. (PIPELINE_DIR resolved to {PIPELINE_DIR} "
        f"-- if PROJECT_ROOT looks wrong, spiders/nh/ may not be exactly two levels under your "
        f"actual project root.)"
    )

try:
    import granit_parcel_downloader     # NH-specific (spiders/nh/)
    import offmarket_value_sweep        # state-agnostic (project root) -- the per-town sweep itself
    import join_parcels_offmarket       # state-agnostic (project root) -- point-in-polygon join
    import offmarket_to_geojson         # state-agnostic (project root) -- QGIS-loadable debug points
except ImportError as e:
    granit_parcel_downloader = None
    offmarket_value_sweep = None
    join_parcels_offmarket = None
    offmarket_to_geojson = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

OFFMARKET_RAW_DIR = "offmarket-raw"  # per-town subdirectory for this town's raw sweep file only

NH_SLU_TO_PROPERTY_TYPE = {
    "11": "Single Family",
    "12": "Two Family",
}

# The real, actual downstream requirement (Thale, 2026-09): gap analysis
# compares a listing's price against nearby comps' values within ~200m.
# A town with no listings at all gets nothing swept, since there'd be no
# possible gap-analysis comparison anywhere in it.
# SIMPLIFIED (2026-09): grid seeded directly from listing coordinates via
# seed_grid() (grid-snap to occupied cells -- no cluster_points()/
# cluster_to_circle() variable-radius clustering anymore; that approach
# was more call-efficient in testing but added real maintenance
# complexity -- union-find clustering, chaining risk, threshold tuning --
# not worth it going forward. This reuses RealtyAPI support's own
# validated recipe parameters (radius=2, split down to 0.25mi) directly,
# just seeded from listing locations instead of blindly tiling a whole
# zip/town -- still skips empty areas, since seed_grid only returns cells
# that actually contain a listing. No separate comp-radius buffer needed
# here either -- a 2-mile cell already covers far more than the 200m
# gap-analysis comp radius on its own.
SEED_RADIUS_MILES = 2.0


def _ring_centroid_from_geojson(geometry: dict | None) -> tuple[float | None, float | None]:
    if not geometry:
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


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class NHSpider(StateSpider):
    state_code = "NH"

    def __init__(self, granit_geojson: str = None, out_dir: str = "data",
                 min_radius: float = 0.25, max_calls: int = 200,
                 listings_file: str = None):
        if _IMPORT_ERROR is not None:
            raise SpiderError(
                f"Could not import the NH pipeline scripts from {PIPELINE_DIR} -- "
                f"make sure all required modules are there. Original error: {_IMPORT_ERROR}"
            )

        self.granit_geojson_override = granit_geojson
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.min_radius = min_radius
        self.max_calls = max_calls
        self.total_api_calls = 0  # accumulates across every fetch_town() call this spider
                                   # instance makes -- lets a multi-town wrapper (e.g.
                                   # sweep_nh_statewide.py) track real cumulative cost across a
                                   # whole session, not just per-town

        if not os.environ.get("REALTYAPI_KEY"):
            raise SpiderError("REALTYAPI_KEY must be set in the environment")

        # Listings-scoped seeding -- a real, accepted dependency on the
        # statewide listings file existing already, since seed_grid() is
        # fed listing coordinates, not parcel data. A town CAN have zero
        # listings at all, in which case there's nothing to seed and
        # nothing gets swept for that town -- intentional, not a bug: no
        # listing means no possible gap-analysis comparison there.
        self.listings_file = listings_file or str(PROJECT_ROOT / "realtyapi-data" / "realtyapi_nh.json")
        if not os.path.exists(self.listings_file):
            raise SpiderError(
                f"requires a statewide listings file at {self.listings_file} "
                f"(or pass listings_file= explicitly) -- run "
                f"`python realtyapi_bypolygon_state.py NH --property-type single_family,land` "
                f"first to generate it."
            )

    def _get_granit_geojson(self, town: str) -> str:
        if self.granit_geojson_override:
            return self.granit_geojson_override
        out_path = self.out_dir / f"{town.lower()}_nh_raw_parcels.geojson"
        print(f"  fetching GRANIT parcels for {town}...")
        features = granit_parcel_downloader.fetch_town_parcels(town)
        geojson = {"type": "FeatureCollection", "features": features}
        with open(out_path, "w") as f:
            json.dump(geojson, f)
        print(f"  wrote {len(features)} GRANIT parcels to {out_path}")
        return str(out_path)

    def _write_debug_points(self, town: str, raw_path) -> None:
        """QGIS-loadable point layer alongside the raw sweep JSON -- the
        raw file itself isn't valid GeoJSON (lat/lon buried under
        location.*, no geometry field), so this is what you'd actually
        load to overlay value points against parcels and verify a match
        rate visually. Skipped silently if it already exists."""
        debug_path = Path(str(raw_path).replace(".json", "_debug.geojson"))
        if debug_path.exists():
            return
        with open(raw_path) as f:
            payload = json.load(f)
        geojson = offmarket_to_geojson.payload_to_geojson(payload)
        with open(debug_path, "w") as f:
            json.dump(geojson, f)
        print(f"  [offmarket] debug point layer ({len(geojson['features'])} point(s)) -> {debug_path}")

    def _load_town_listing_points(self, town: str) -> list:
        """Coordinates of every active listing in this town, from the
        statewide listings file -- fed directly into seed_grid() to
        decide which grid cells are worth sweeping, not loaded into the
        DB from here (that's a separate step, load_listings_realtyapi.py)."""
        with open(self.listings_file) as f:
            payload = json.load(f)
        records = payload if isinstance(payload, list) else payload.get("searchResults", [])
        return [
            (r["address"]["latitude"], r["address"]["longitude"])
            for r in records
            if (r.get("address", {}).get("city") or "").lower() == town.lower()
            and r.get("address", {}).get("latitude") is not None
        ]

    def _sweep_offmarket_for_town(self, town: str, granit_geojson_path: str) -> str:
        """
        Per-town offmarket sweep: build a seed grid from this town's real
        parcel extent, sweep each seed to confirmed completeness (or the
        --max-calls budget, whichever comes first), write the raw results
        to this town's own dedicated subdirectory (never shared with
        other towns' files, so join_parcels_offmarket's directory-glob
        loader can't pick up the wrong town's data). Returns the path to
        that subdirectory, ready for load_offmarket_points().

        CACHED: if this town's raw sweep file already exists, skips the
        sweep entirely and reuses it -- no new API calls. Given the
        confirmed tight RealtyAPI budget (2,000 calls/month), re-running
        the same town twice must NOT silently burn another ~35+ calls for
        identical data. Delete the file manually (or the whole
        OFFMARKET_RAW_DIR/<town>/ folder) to force a genuine re-sweep.
        """
        raw_dir = self.out_dir / OFFMARKET_RAW_DIR / town.lower()
        raw_path = raw_dir / f"{town.lower()}_nh_offmarket_raw.json"

        if raw_path.exists():
            with open(raw_path) as f:
                cached = json.load(f)
            n_cached = len(cached.get("offMarketResults", []))
            print(f"  [offmarket] {town}: reusing existing sweep at {raw_path} "
                  f"({n_cached} record(s), 0 new API calls). Delete this file to force a re-sweep.")
            self._write_debug_points(town, raw_path)
            return str(raw_dir)

        api_key = os.environ.get("REALTYAPI_KEY")

        listing_points = self._load_town_listing_points(town)
        seeds = offmarket_value_sweep.seed_grid(listing_points, SEED_RADIUS_MILES)
        print(f"  [offmarket] {len(listing_points)} listing(s) in {town} -> {len(seeds)} "
              f"occupied grid cell(s) at {SEED_RADIUS_MILES}mi")

        if not seeds:
            print(f"  [offmarket] {town}: no listings, nothing to sweep -- skipping entirely")
            raw_dir.mkdir(parents=True, exist_ok=True)
            with open(raw_path, "w") as f:
                json.dump({"offMarketResults": []}, f)
            return str(raw_dir)

        seen_zpids, out_records, incomplete_areas, call_counter = set(), [], [], [0]
        visited_cells = set()

        for i, (lat, lon) in enumerate(seeds, 1):
            print(f"  [offmarket] cell {i}/{len(seeds)} for {town}...")
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
        print(f"  [offmarket] {town}: {status} -- {call_counter[0]} call(s), "
              f"{len(out_records)} unique record(s)"
              + (f", {len(incomplete_areas)} area(s) not confirmed complete" if incomplete_areas else ""))
        self.total_api_calls += call_counter[0]

        raw_dir.mkdir(parents=True, exist_ok=True)
        with open(raw_path, "w") as f:
            json.dump({"offMarketResults": out_records}, f)
        self._write_debug_points(town, raw_path)

        return str(raw_dir)

    def _normalize_feature(self, feature: dict, town: str) -> dict:
        props = feature.get("properties", {})
        geometry = feature.get("geometry")
        lat, lon = _ring_centroid_from_geojson(geometry)
        pid = props.get("PID")
        # FIXED 2026-09: property_id used to build its town segment from the
        # raw `town` argument passed into this run (CLI-typed or however the
        # caller cased it), while `municipality` below already preferred
        # GRANIT's own Town property instead. The two callers of fetch_town()
        # in this project (sweep_nh_statewide.py, reading town names from
        # newengland_town_boundaries.json, vs. a manual `python -m
        # spiders.nh.nh_spider <Town>` CLI run) don't always agree on casing
        # for the same town -- CONFIRMED: Freedom loaded as both
        # "NH:freedom:2-2" (statewide sweep) and "NH:Freedom:2-2" (manual
        # re-run), producing two separate DB rows for one physical parcel
        # instead of the second upsert overwriting the first. Resolving
        # municipality ONCE here and using that same value for both fields
        # closes the gap -- property_id's town segment now always matches
        # GRANIT's own casing, regardless of how the caller typed/sourced
        # the town name.
        municipality = props.get("Town") or town
        record = {
            "property_id": f"NH:{municipality}:{pid}" if pid else None,
            "state": "NH",
            "county": None,
            "municipality": municipality,
            "parcel_id": pid,
            # offmarket_address only ever gets set on a real match (see
            # join_parcels_offmarket.py) -- now that matches are strict
            # point-in-polygon only, it always belongs to a point
            # genuinely inside this parcel. Falls back to GRANIT's own
            # StreetAddress when there was no match at all.
            "address": props.get("offmarket_address") or props.get("StreetAddress"),
            "city": municipality,
            "zip": None,
            "latitude": lat,
            "longitude": lon,
            "acreage": _to_float(props.get("offmarket_acres")),
            "assessed_value": _to_float(props.get("total_market_value")),
            "assessed_land_value": None,
            "assessed_building_value": None,
            "assessment_year": props.get("tax_assessment_year"),
            "last_sale_price": None,
            "last_sale_date": None,
            "building_sqft": None,
            "bedrooms": None,
            "bathrooms": None,
            "year_built": None,
            "property_type": NH_SLU_TO_PROPERTY_TYPE.get(props.get("SLU")),
            "source": "NH_GRANIT_OFFMARKET",
            "source_url": None,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        return record

    def fetch_town(self, town: str) -> list[dict]:
        granit_geojson_path = self._get_granit_geojson(town)

        offmarket_dir = self._sweep_offmarket_for_town(town, granit_geojson_path)
        joined_geojson = str(self.out_dir / f"{town.lower()}_nh_offmarket_joined.geojson")

        with open(Path(offmarket_dir) / f"{town.lower()}_nh_offmarket_raw.json") as f:
            raw_count = len(json.load(f).get("offMarketResults", []))

        if raw_count == 0:
            # No listings in this town -> nothing was swept, intentionally (see
            # _sweep_offmarket_for_town). Pass GRANIT parcels through with null value
            # fields instead of calling load_offmarket_points, which raises on zero
            # points -- this is expected here, not a real failure.
            print(f"  [offmarket] {town}: 0 swept records, passing parcels through with "
                  f"null values (no listings to compare against in this town)")
            with open(granit_geojson_path) as f:
                parcels = json.load(f)
            for feature in parcels["features"]:
                for field in ("total_market_value", "offmarket_zpid", "offmarket_address",
                              "tax_assessed_value", "tax_assessment_year", "match_method",
                              "match_point_count"):
                    feature["properties"].setdefault(field, None if field != "match_point_count" else 0)
            with open(joined_geojson, "w") as f:
                json.dump(parcels, f)
        else:
            points = join_parcels_offmarket.load_offmarket_points(offmarket_dir)
            tree = join_parcels_offmarket.build_index(points)
            print(f"  joining GRANIT parcels + offmarket points for {town}...")
            join_parcels_offmarket.join_town_file(granit_geojson_path, tree, points, joined_geojson)

        with open(joined_geojson) as f:
            joined = json.load(f)
        return [self._normalize_feature(feat, town) for feat in joined["features"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="+", help="NH town names, e.g. Lincoln")
    parser.add_argument("--granit-geojson", help="single-town pre-downloaded GRANIT geojson override")
    parser.add_argument("--min-radius", type=float, default=0.25)
    parser.add_argument("--max-calls", type=int, default=200,
                         help="PER-TOWN budget, not statewide")
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    if args.granit_geojson and len(args.towns) > 1:
        print("ERROR: --granit-geojson only makes sense for a single town")
        sys.exit(1)

    spider = NHSpider(
        granit_geojson=args.granit_geojson,
        out_dir=args.out,
        min_radius=args.min_radius,
        max_calls=args.max_calls,
    )
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()