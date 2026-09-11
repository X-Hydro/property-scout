"""
New Hampshire spider — Property Values Database

TWO value sources, selected via value_source:

  - "vgsi" (default, unchanged): GRANIT parcel geometry + VGSI assessed
    value, joined on MBLU. Original path, per-town VGSI scraping.

  - "offmarket": GRANIT parcel geometry + RealtyAPI/Zillow off-market
    values, joined via point-in-polygon. Built to route around VGSI's
    fragile per-town scraping, since NH has no single authoritative
    statewide assessed-value source.

    CONFIRMED (2026-09): a single zipCode-based /search/offmarket call is
    a SAMPLE, not a complete set (RealtyAPI support's own words) --
    measured ~85% of a real cluster missing on one small NH zip. The
    reliable approach is recursive radius-based splitting (pageResultCount
    hitting 1000 means the area needs splitting into 4 sub-cells; below
    1000 means genuinely complete for that area), tiled across a town's
    real parcel extent -- implemented in offmarket_value_sweep.py (project root,
    state-agnostic, no NH-specific code in it).

    RUNS ENTIRELY PER-TOWN, inside fetch_town() -- unlike an earlier
    version of this file, there is NO state-level prep step in __init__
    anymore. The old design fetched a statewide listings file first
    (to pick which zip codes to sweep); the new approach needs a town's
    PARCELS first (to build its seed grid), which fetch_town() already
    gets via _get_granit_geojson() before anything offmarket-related runs.

    BUDGET, CONFIRMED CONSTRAINT (2026-09): RealtyAPI plan is capped at
    2,000 calls/month. A rough per-town estimate (56-105 calls seen on
    Lebanon/Lincoln, before a caching fix that reduced Lincoln's real
    cost -- re-test recommended) times ~234 NH towns is WAY over that
    budget for a single statewide pass. Two known mitigations, NEITHER
    implemented here yet: (1) scope to only towns with active listings,
    not all 234 (see conversation -- most towns may have none); (2) a
    resumable checkpoint so a statewide sweep spans several months'
    budget instead of needing to fit in one. For now, run town-by-town
    deliberately (--towns X), not --all-towns, until one of those exists.

Usage:
    # Original VGSI path, unchanged:
    python -m spiders.nh_spider Lincoln --town-slug lincolnnh --pid-end 20000 --out data/

    # New offmarket path, per-town parcel-extent sweep:
    python -m spiders.nh_spider Lincoln --value-source offmarket --out data/
    python -m spiders.nh_spider Lincoln --value-source offmarket --seed-radius 4 --max-calls 150 --out data/
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import date

from ..common.base import StateSpider, SpiderError

PIPELINE_DIR = Path(__file__).parent          # spiders/nh/ -- NH-SPECIFIC: GRANIT fetch, VGSI scraper, MBLU join
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
    import vgsi_assessment_scraper      # NH-specific (spiders/nh/) -- required import even when
                                         # running the offmarket path; not called, but this file's
                                         # top-level import block is unconditional either way
    import join_parcels_assessments     # NH-specific (spiders/nh/) -- VGSI's MBLU join
    import offmarket_value_sweep                 # state-agnostic (project root) -- the per-town sweep itself
    import join_parcels_offmarket       # state-agnostic (project root) -- point-in-polygon join
    import offmarket_to_geojson         # state-agnostic (project root) -- QGIS-loadable debug points
except ImportError as e:
    granit_parcel_downloader = None
    vgsi_assessment_scraper = None
    join_parcels_assessments = None
    offmarket_value_sweep = None
    join_parcels_offmarket = None
    offmarket_to_geojson = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

OFFMARKET_RAW_DIR = "offmarket-raw"  # per-town subdirectory for this town's raw sweep file only


def _guess_vgsi_town_slug(town: str) -> str:
    return town.lower().replace(" ", "") + "nh"


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

    def __init__(self, granit_geojson: str = None, town_slug: str = None,
                 pid_end: int = 20000, out_dir: str = "data",
                 value_source: str = "vgsi",
                 seed_radius: float = 2.0, min_radius: float = 0.25, max_calls: int = 200):
        if _IMPORT_ERROR is not None:
            raise SpiderError(
                f"Could not import the NH pipeline scripts from {PIPELINE_DIR} -- "
                f"make sure all required modules are there. Original error: {_IMPORT_ERROR}"
            )
        if value_source not in ("vgsi", "offmarket"):
            raise SpiderError(f"value_source must be 'vgsi' or 'offmarket', got {value_source!r}")

        self.granit_geojson_override = granit_geojson
        self.town_slug_override = town_slug
        self.pid_end = pid_end
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.value_source = value_source
        self.seed_radius = seed_radius
        self.min_radius = min_radius
        self.max_calls = max_calls
        self.total_api_calls = 0  # accumulates across every fetch_town() call this spider
                                   # instance makes -- lets a multi-town wrapper (e.g.
                                   # sweep_nh_statewide.py) track real cumulative cost across a
                                   # whole session, not just per-town

        if value_source == "offmarket" and not os.environ.get("REALTYAPI_KEY"):
            raise SpiderError("value_source='offmarket' requires REALTYAPI_KEY set in the environment")

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

        centroids = offmarket_value_sweep.parcel_centroids(granit_geojson_path)
        if not centroids:
            raise SpiderError(f"No usable parcel centroids in {granit_geojson_path} for {town}")

        seeds = offmarket_value_sweep.seed_grid(centroids, self.seed_radius)
        print(f"  [offmarket] {len(centroids)} parcel centroid(s) -> {len(seeds)} seed cell(s) "
              f"at {self.seed_radius}mi for {town}")

        seen_zpids, out_records, incomplete_areas, call_counter = set(), [], [], [0]
        visited_cells = set()

        for i, (lat, lon) in enumerate(seeds, 1):
            print(f"  [offmarket] seed {i}/{len(seeds)} for {town}...")
            if call_counter[0] >= self.max_calls:
                print(f"    SKIPPED -- --max-calls {self.max_calls} budget exhausted for this town")
                incomplete_areas.append((lat, lon, self.seed_radius, "ABORTED-BUDGET"))
                continue
            offmarket_value_sweep.sweep_complete(
                lat, lon, self.seed_radius, self.min_radius, api_key,
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
        record = {
            "property_id": f"NH:{pid}" if pid else None,
            "state": "NH",
            "county": None,
            "municipality": props.get("Town") or town,
            "parcel_id": pid,
            "address": props.get("vgsi_location") or props.get("offmarket_address") or props.get("StreetAddress"),
            "city": props.get("Town") or town,
            "zip": None,
            "latitude": lat,
            "longitude": lon,
            "acreage": _to_float(props.get("acres")),
            "assessed_value": _to_float(props.get("total_market_value")),
            "assessed_land_value": None,
            "assessed_building_value": None,
            "assessment_year": props.get("tax_assessment_year"),  # populated on the offmarket path only
            "last_sale_price": None,
            "last_sale_date": None,
            "building_sqft": None,
            "bedrooms": None,
            "bathrooms": None,
            "year_built": None,
            "property_type": props.get("land_use_desc"),
            "source": "NH_GRANIT_VGSI" if self.value_source == "vgsi" else "NH_GRANIT_OFFMARKET",
            "source_url": None,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        return record

    def fetch_town(self, town: str) -> list[dict]:
        granit_geojson_path = self._get_granit_geojson(town)

        if self.value_source == "offmarket":
            offmarket_dir = self._sweep_offmarket_for_town(town, granit_geojson_path)
            points = join_parcels_offmarket.load_offmarket_points(offmarket_dir)
            tree = join_parcels_offmarket.build_index(points)
            joined_geojson = str(self.out_dir / f"{town.lower()}_nh_offmarket_joined.geojson")
            print(f"  joining GRANIT parcels + offmarket points for {town}...")
            join_parcels_offmarket.join_town_file(granit_geojson_path, tree, points, joined_geojson)
        else:
            town_slug = self.town_slug_override or _guess_vgsi_town_slug(town)
            assessments_csv = str(self.out_dir / f"{town.lower()}_nh_assessments.csv")
            joined_geojson = str(self.out_dir / f"{town.lower()}_nh_joined.geojson")
            print(f"  scraping VGSI ({town_slug}, pid_end={self.pid_end})...")
            vgsi_assessment_scraper.scrape_town(
                town_slug, 1, self.pid_end, granit_geojson_path, assessments_csv
            )
            print("  joining GRANIT + VGSI...")
            join_parcels_assessments.join(granit_geojson_path, assessments_csv, joined_geojson)

        with open(joined_geojson) as f:
            joined = json.load(f)
        return [self._normalize_feature(feat, town) for feat in joined["features"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="+", help="NH town names, e.g. Lincoln")
    parser.add_argument("--granit-geojson", help="single-town pre-downloaded GRANIT geojson override")
    parser.add_argument("--town-slug", help="VGSI town slug override, e.g. lincolnnh (vgsi path only)")
    parser.add_argument("--pid-end", type=int, default=20000)
    parser.add_argument("--value-source", choices=["vgsi", "offmarket"], default="vgsi")
    parser.add_argument("--seed-radius", type=float, default=2.0, help="offmarket path only")
    parser.add_argument("--min-radius", type=float, default=0.25, help="offmarket path only")
    parser.add_argument("--max-calls", type=int, default=200,
                         help="offmarket path only -- PER-TOWN budget, not statewide")
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    if args.granit_geojson and len(args.towns) > 1:
        print("ERROR: --granit-geojson only makes sense for a single town")
        sys.exit(1)

    spider = NHSpider(
        granit_geojson=args.granit_geojson,
        town_slug=args.town_slug,
        pid_end=args.pid_end,
        out_dir=args.out,
        value_source=args.value_source,
        seed_radius=args.seed_radius,
        min_radius=args.min_radius,
        max_calls=args.max_calls,
    )
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()