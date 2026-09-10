"""
New Hampshire spider — Property Values Database

TWO value sources now supported, selected via value_source:

  - "vgsi" (default, unchanged): GRANIT parcel geometry + VGSI assessed
    value, joined on MBLU. Original path, still per-town VGSI scraping.

  - "offmarket" (NEW): GRANIT parcel geometry + RealtyAPI/Zillow
    /search/offmarket values, joined via point-in-polygon. Built because
    VGSI's per-town scraping is NH's most fragile pipeline piece, and
    NH has no single authoritative statewide assessed-value source to
    replace it with the way VT/NJ/MA have -- this routes around VGSI
    entirely using Zillow's off-market estimates + tax assessments
    instead. See join_parcels_offmarket.py's docstring for the matching
    strategy and VALUE_PRIORITY for which field (tax_assessed_value vs.
    zestimate) becomes total_market_value.

    STEPS 1-4 OF THE OFFMARKET PATH RUN ONCE PER STATE, IN __init__ --
    NOT per town. This mirrors how run_spiders.py already constructs the
    spider once outside its per-town loop, and matches why
    join_parcels_offmarket.py builds one combined spatial index across
    every swept zip rather than a separate one per town: a zip's
    off-market points aren't scoped to a single town, so there's nothing
    town-specific to redo per town. fetch_town() for the offmarket path
    only does steps 5-7 (GRANIT fetch + point-in-polygon join +
    normalize) per town, reusing the shared index built once.

    SUBPROCESS CAVEAT (offmarket path only, listings fetch specifically):
    calls realtyapi_bypolygon_state.py via subprocess rather than direct
    import, unlike every other call in this file (GRANIT/VGSI/join are
    all direct imports, per this file's own "no subprocess" convention
    above). This is a deliberate, flagged exception, not an oversight --
    that script's internal functions (load_state_geometry, select_parts,
    search_polygon, dedup helpers) were not fully confirmed/reviewed at
    the time this was written. Revisit and switch to a direct import
    once those signatures are confirmed; subprocess is the honest
    stopgap until then, not the intended final shape.

REQUIRES (offmarket path, in addition to the vgsi-path requirements):
offmarket_sweep.py, select_offmarket_zips.py, and join_parcels_offmarket.py
at the PROJECT ROOT (not spiders/nh/ -- these are state-agnostic, reusable
by any other state that lacks a single authoritative value source), plus
realtyapi_bypolygon_state.py runnable via subprocess with REALTYAPI_KEY set
in the environment.

Usage:
    # Original VGSI path, unchanged:
    python -m spiders.nh_spider Lincoln --town-slug lincolnnh --pid-end 20000 --out data/

    # New offmarket path:
    python -m spiders.nh_spider Lincoln --value-source offmarket --out data/
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import date

from ..common.base import StateSpider, SpiderError

PIPELINE_DIR = Path(__file__).parent  # spiders/nh/ -- NH-SPECIFIC only: GRANIT fetch, VGSI scraper, MBLU join
PROJECT_ROOT = PIPELINE_DIR.parent.parent  # project root -- STATE-AGNOSTIC RealtyAPI/offmarket tooling,
                                            # reusable by any other state that ever needs the same
                                            # no-authoritative-source workaround (join_parcels_offmarket.py,
                                            # select_offmarket_zips.py, offmarket_sweep.py all live here,
                                            # alongside realtyapi_bypolygon_state.py and
                                            # load_listings_realtyapi.py -- NOT bundled with NH-only code)
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

# Explicit existence check BEFORE importing, so a missing/misplaced file
# fails with a clear "here's exactly what's missing and where I looked"
# message instead of Python's generic "No module named X" -- which tells
# you a name couldn't be resolved somewhere on sys.path, not WHICH path
# was checked or what file was expected there.
_REQUIRED_ROOT_FILES = ["join_parcels_offmarket.py", "select_offmarket_zips.py",
                         "offmarket_sweep.py", "realtyapi_response_utils.py",
                         "realtyapi_bypolygon_state.py"]
# NOTE: load_listings_realtyapi.py / listings_db.py deliberately NOT required here --
# the offmarket path only needs to PARSE the listings file (realtyapi_response_utils.py),
# never to write to the DB. load_listings_realtyapi.py is still a separate, later pipeline
# step (run standalone, step 3 of the NH flow) -- it has its own listings_db.py dependency,
# unrelated to this spider's internals.
_missing_root_files = [f for f in _REQUIRED_ROOT_FILES if not (PROJECT_ROOT / f).exists()]
if _missing_root_files:
    raise SpiderError(
        f"Expected these state-agnostic files directly under PROJECT_ROOT "
        f"({PROJECT_ROOT}), but they're missing: {_missing_root_files}. "
        f"(PIPELINE_DIR resolved to {PIPELINE_DIR} -- if PROJECT_ROOT looks "
        f"wrong above, the spiders/nh/ directory may not be exactly two "
        f"levels under your actual project root, and PROJECT_ROOT's "
        f"computation needs adjusting.)"
    )

try:
    import granit_parcel_downloader     # NH-specific (spiders/nh/)
    import vgsi_assessment_scraper      # NH-specific (spiders/nh/)
    import join_parcels_assessments     # NH-specific (spiders/nh/) -- VGSI's MBLU join
    import join_parcels_offmarket       # state-agnostic (project root)
    import offmarket_sweep              # state-agnostic (project root)
    import select_offmarket_zips        # state-agnostic (project root)
    import realtyapi_response_utils     # state-agnostic (project root) -- DEPENDENCY-FREE parsing
                                         # helpers (no listings_db.py needed); replaces an earlier
                                         # attempt to import load_listings_realtyapi directly just
                                         # for _extract_records, which unnecessarily pulled in that
                                         # module's DB-write dependencies for a pure parsing step
except ImportError as e:
    granit_parcel_downloader = None
    vgsi_assessment_scraper = None
    join_parcels_assessments = None
    join_parcels_offmarket = None
    offmarket_sweep = None
    select_offmarket_zips = None
    realtyapi_response_utils = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

# Where realtyapi_bypolygon_state.py's subprocess output and the offmarket
# sweep's per-zip files land -- named distinctly from the VGSI path's
# per-town intermediates (which live directly under out_dir), so a
# directory listing makes clear which files belong to which value_source.
REALTYAPI_DATA_DIR = "realtyapi-data"
OFFMARKET_DATA_DIR = "offmarket-data"
OFFMARKET_DEBUG_DIR = "offmarket-debug"  # QGIS-loadable point geojson per swept zip --
                                          # kept SEPARATE from OFFMARKET_DATA_DIR so
                                          # load_offmarket_points()'s directory glob in a
                                          # full statewide run never has to reason about
                                          # whether a debug file counts as sweep data
MIN_OFFMARKET_ZIP_LISTING_COUNT = 10  # matches the ">10 active listings" threshold already agreed on


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
                 value_source: str = "vgsi", offmarket_zips: list[str] = None):
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

        self.offmarket_points = None
        self.offmarket_tree = None
        self.offmarket_zips = offmarket_zips
        if value_source == "offmarket":
            self._prepare_offmarket_source()

    def _prepare_offmarket_source(self):
        """Runs once, in __init__ -- steps 1-4 of the offmarket path (see
        module docstring for why these are state-scoped, not per-town).

        SCOPED-ZIP MODE: if self.offmarket_zips is set, steps 1-2
        (statewide listings fetch + zip selection) are skipped entirely --
        just sweeps the given zip(s) directly. This IS a legitimate
        production mode, not just for testing -- e.g. refreshing one town
        after new listings came in there, without re-running the full
        statewide sweep. The tradeoff: you're taking over the job the
        automatic zip-selection step normally does, which means YOU are
        responsible for knowing which zip(s) actually cover the town(s)
        you're refreshing -- a town spanning multiple zips needs all of
        them passed, or the join will be silently partial for whatever
        zip(s) you left out. Fine for a well-known small town (Lincoln is
        entirely zip 03251); riskier for anything you're less sure about."""
        api_key = os.environ.get("REALTYAPI_KEY")
        if not api_key:
            raise SpiderError("value_source='offmarket' requires REALTYAPI_KEY set in the environment")

        offmarket_dir = self.out_dir / OFFMARKET_DATA_DIR
        offmarket_debug_dir = self.out_dir / OFFMARKET_DEBUG_DIR

        if self.offmarket_zips:
            print(f"  [offmarket] SCOPED-ZIP MODE: sweeping only {self.offmarket_zips} -- "
                  f"skipping statewide listings fetch and zip selection. Make sure this covers "
                  f"every zip the town(s) you're running actually span, or coverage will be "
                  f"silently partial.")
            succeeded, failed = offmarket_sweep.sweep_zips(
                self.offmarket_zips, str(offmarket_dir), api_key,
                debug_geojson_dir=str(offmarket_debug_dir)
            )
            if failed:
                raise SpiderError(f"offmarket sweep failed for zip(s): {failed}")
        else:
            realtyapi_dir = self.out_dir / REALTYAPI_DATA_DIR
            realtyapi_dir.mkdir(parents=True, exist_ok=True)
            listings_path = realtyapi_dir / "realtyapi_nh.json"

            # Step 1: fetch statewide listings -- SKIPPED if the file already
            # exists, so re-running against the same --out during testing
            # doesn't re-burn a ~28-page statewide fetch every time. Delete
            # the file manually to force a real re-fetch (e.g. before a real
            # production run, not just a single-town test).
            if listings_path.exists():
                print(f"  [offmarket] reusing existing listings file {listings_path} "
                      f"(delete it to force a re-fetch)")
            else:
                print("  [offmarket] fetching NH statewide listings...")
                import subprocess
                result = subprocess.run(
                    [sys.executable, str(PROJECT_ROOT / "realtyapi_bypolygon_state.py"),
                     "NH", "--property-type", "single_family,land", "--out", str(listings_path)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise SpiderError(f"realtyapi_bypolygon_state.py failed:\n{result.stdout}\n{result.stderr}")
                print(f"  [offmarket] listings written to {listings_path}")

            # Step 2: select qualifying zips from that same file -- no DB
            # dependency, see select_offmarket_zips.py's docstring.
            with open(listings_path) as f:
                payload = json.load(f)
            records = realtyapi_response_utils.extract_records(payload)
            property_types = {"Single Family", "Land"}
            qualifying_zips, counts = select_offmarket_zips.select_zips(
                records, MIN_OFFMARKET_ZIP_LISTING_COUNT, property_types
            )
            print(f"  [offmarket] {len(counts)} distinct zip(s) seen, "
                  f"{len(qualifying_zips)} qualify (> {MIN_OFFMARKET_ZIP_LISTING_COUNT} active listings)")

            # Step 3: sweep /search/offmarket for each qualifying zip.
            succeeded, failed = offmarket_sweep.sweep_zips(
                qualifying_zips, str(offmarket_dir), api_key,
                debug_geojson_dir=str(offmarket_debug_dir)
            )
            if failed:
                print(f"  [offmarket] WARNING: {len(failed)} zip(s) failed to sweep -- "
                      f"{[z for z, _ in failed]} -- proceeding with {len(succeeded)} successful zip(s). "
                      f"Coverage for towns in the failed zips will be incomplete this run.")
            if not succeeded:
                raise SpiderError("offmarket sweep: every zip failed, nothing to join against")

        # Step 4: build ONE combined point index across every swept zip,
        # shared by every town's fetch_town() call below. Pass
        # self.offmarket_zips (None for a full statewide run, meaning
        # "use every file present" -- a real zip list for a scoped run,
        # meaning "use ONLY these files, ignore anything else sitting in
        # offmarket_dir from an earlier run" -- see
        # join_parcels_offmarket.load_offmarket_points' docstring for why
        # this matters (offmarket_dir accumulates across runs by design).
        print("  [offmarket] building combined offmarket point index...")
        self.offmarket_points = join_parcels_offmarket.load_offmarket_points(
            str(offmarket_dir), zip_codes=self.offmarket_zips
        )
        if not self.offmarket_points:
            raise SpiderError("offmarket sweep produced zero usable points -- nothing to join against")
        self.offmarket_tree = join_parcels_offmarket.build_index(self.offmarket_points)

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
            joined_geojson = str(self.out_dir / f"{town.lower()}_nh_offmarket_joined.geojson")
            print(f"  joining GRANIT parcels + offmarket points for {town}...")
            join_parcels_offmarket.join_town_file(
                granit_geojson_path, self.offmarket_tree, self.offmarket_points, joined_geojson
            )
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
    parser.add_argument("--offmarket-zip", nargs="+",
                         help="Skip the statewide listings-fetch + zip-selection step and sweep "
                              "only these zip(s) directly. Useful for a full statewide run's fast "
                              "iteration, or for a deliberate scoped refresh of one town (e.g. "
                              "--offmarket-zip 03251 for Lincoln) -- you're responsible for "
                              "knowing which zip(s) actually cover the town(s) you're running.")
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
        offmarket_zips=args.offmarket_zip,
    )
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()