"""
New Hampshire spider — Property Values Database

Unlike CT (one clean REST API call), NH's data comes from TWO real
sources, both now called directly -- no subprocess, no unverified CLI
guessing:
  - granit_parcel_downloader.fetch_town_parcels()  (parcel geometry +
    StreetAddress, direct ArcGIS REST query -- same pattern as
    ct_spider.py querying CT's API directly)
  - vgsi_assessment_scraper.scrape_town()  (assessed value, Pass 1
    sequential + Pass 2 targeted-address-lookup fallback)
  - join_parcels_assessments.join()         (collision-safe 4-component
    MBLU key -- the condo-unit-collision fix; now also a LEFT join, see
    that module's docstring -- a GRANIT parcel with no VGSI match still
    keeps its geometry, just with null value fields)
This spider does not reimplement any of that -- it imports and calls the
real, already-fixed functions, and only adds a final step: read the
joined GeoJSON and remap its fields into COMMON_SCHEMA_FIELDS.

REQUIRES: granit_parcel_downloader.py, vgsi_assessment_scraper.py,
vgsi_targeted_lookup.py, and join_parcels_assessments.py importable --
this adds their directory to sys.path, so keep them together with this
file's parent, or edit PIPELINE_DIR below.

IMPORTANT FIELD-COVERAGE GAP vs. CT: the current VGSI scraper only
parses Total Market Value and land_use_desc -- it does NOT capture
land/building value split, assessment year, sale price/date, bedrooms,
bathrooms, or year_built, even though VGSI's own pages likely display
some of these. So NH records will have noticeably more None fields than
CT's after normalization. This is a real asymmetry, not a bug -- worth
knowing before assuming both states will look equally complete in
Postgres. Extending parse_parcel() in vgsi_assessment_scraper.py to
capture more fields is a separate, deliberate future task, not done here.

FIXED: intermediate files (<town>_nh.geojson, <town>_assessments.csv,
<town>_joined.geojson) previously wrote to bare relative filenames --
meaning they landed in the process's current working directory,
completely ignoring --out. Silent and easy to miss (the final normalized
records still went to --out via StateSpider.run(), so nothing looked
obviously broken), but it meant a run's own intermediate artifacts
weren't actually kept with everything else that run produced, and could
even get overwritten by a later run for a different town before anyone
noticed. Now takes out_dir at construction (same pattern as
granit_geojson/town_slug/pid_end -- see run_ingest.py's SPIDER_KWARGS)
and writes every intermediate file under it.

Usage:
    python -m spiders.nh_spider Lincoln --town-slug lincolnnh --pid-end 20000 --out data/
    python -m spiders.nh_spider Lincoln --granit-geojson lincoln_nh.geojson \\
        --town-slug lincolnnh --pid-end 20000 --out data/   # skip live GRANIT fetch, reuse a file
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import date

from ..common.base import StateSpider, SpiderError

# Directory containing the NH pipeline scripts (granit_parcel_downloader.py,
# vgsi_assessment_scraper.py, vgsi_targeted_lookup.py,
# join_parcels_assessments.py) -- these live alongside this file, inside
# spiders/, not in the project root. Added to sys.path (not just relied on
# as a package) so these scripts stay runnable standalone from the command
# line too (e.g. `cd spiders && python vgsi_assessment_scraper.py ...`),
# which is how they've been used throughout this project -- switching them
# to relative package imports would break that.
PIPELINE_DIR = Path(__file__).parent
sys.path.insert(0, str(PIPELINE_DIR))

try:
    import granit_parcel_downloader  # real GRANIT fetch, direct ArcGIS query
    import vgsi_assessment_scraper  # the fixed, two-pass version
    import join_parcels_assessments  # the fixed, collision-safe, left-join version
except ImportError as e:
    granit_parcel_downloader = None
    vgsi_assessment_scraper = None
    join_parcels_assessments = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


def _guess_vgsi_town_slug(town: str) -> str:
    """Best-effort 'Lincoln' -> 'lincolnnh' convention, matching Lincoln/
    Lebanon so far. UNVERIFIED for towns with spaces or unusual names --
    pass --town-slug explicitly if this guess is wrong."""
    return town.lower().replace(" ", "") + "nh"


def _ring_centroid_from_geojson(geometry: dict | None) -> tuple[float | None, float | None]:
    """Same rough-centroid approach as ct_spider.py, adapted for GeoJSON
    (not Esri) coordinate structure -- GRANIT geometry is already GeoJSON."""
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


class NHSpider(StateSpider):
    state_code = "NH"

    def __init__(self, granit_geojson: str = None, town_slug: str = None,
                 pid_end: int = 20000, out_dir: str = "data"):
        if _IMPORT_ERROR is not None:
            raise SpiderError(
                f"Could not import the NH pipeline scripts from {PIPELINE_DIR} -- "
                f"make sure vgsi_assessment_scraper.py, vgsi_targeted_lookup.py, and "
                f"join_parcels_assessments.py are there. Original error: {_IMPORT_ERROR}"
            )
        self.granit_geojson_override = granit_geojson
        self.town_slug_override = town_slug
        self.pid_end = pid_end
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _get_granit_geojson(self, town: str) -> str:
        if self.granit_geojson_override:
            return self.granit_geojson_override
        # Direct call, same pattern as ct_spider.py -- no subprocess.
        out_path = self.out_dir / f"{town.lower()}_nh.geojson"
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
            "county": None,  # not present in GRANIT's confirmed field list
            "municipality": props.get("Town") or town,
            "parcel_id": pid,
            "address": props.get("vgsi_location") or props.get("StreetAddress"),
            "city": props.get("Town") or town,
            "zip": None,
            "latitude": lat,
            "longitude": lon,
            "acreage": _to_float(props.get("acres")),
            "assessed_value": _to_float(props.get("total_market_value")),
            "assessed_land_value": None,  # not captured by the current VGSI scraper -- see module docstring
            "assessed_building_value": None,
            "assessment_year": None,
            "last_sale_price": None,
            "last_sale_date": None,
            "building_sqft": None,
            "bedrooms": None,
            "bathrooms": None,
            "year_built": None,
            "property_type": props.get("land_use_desc"),
            "source": "NH_GRANIT_VGSI",
            "source_url": None,
            "source_date": date.today().isoformat(),
            "_geometry": geometry,
        }
        return record

    def fetch_town(self, town: str) -> list[dict]:
        town_slug = self.town_slug_override or _guess_vgsi_town_slug(town)
        granit_geojson_path = self._get_granit_geojson(town)

        assessments_csv = str(self.out_dir / f"{town.lower()}_assessments.csv")
        joined_geojson = str(self.out_dir / f"{town.lower()}_joined.geojson")

        print(f"  scraping VGSI ({town_slug}, pid_end={self.pid_end})...")
        vgsi_assessment_scraper.scrape_town(
            town_slug, 1, self.pid_end, granit_geojson_path, assessments_csv
        )

        print("  joining GRANIT + VGSI...")
        join_parcels_assessments.join(granit_geojson_path, assessments_csv, joined_geojson)

        with open(joined_geojson) as f:
            joined = json.load(f)

        return [self._normalize_feature(feat, town) for feat in joined["features"]]


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("towns", nargs="+", help="NH town names, e.g. Lincoln")
    parser.add_argument("--granit-geojson", help="use an already-downloaded GRANIT geojson "
                                                    "instead of running granit_parcel_downloader.py "
                                                    "(only valid for a single town)")
    parser.add_argument("--town-slug", help="VGSI town slug override, e.g. lincolnnh")
    parser.add_argument("--pid-end", type=int, default=20000)
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
    )
    spider.run(args.towns, args.out)


if __name__ == "__main__":
    main()