"""
Base spider interface — Property Values Database

Every state's data comes from a mechanically different source (CT: a
paginated ArcGIS REST API; NH: the existing GRANIT+VGSI scrape/join
pipeline; MA/VT/RI/ME: TBD, likely bulk file downloads or per-town
pulls). This base class does NOT try to unify *how* each spider fetches
data -- that has to stay source-specific. What it unifies is what each
spider hands back: every spider's fetch_town() must yield records
already normalized into COMMON_SCHEMA_FIELDS, so downstream (GeoJSON
output now, Postgres load later) never needs to know which state or
source a record came from.

Two lessons carried over deliberately from the AuctionScout spider
architecture (spiders/base.py there), since both already cost real
debugging time in that codebase:
  1. Row-count sanity checking, not just fetching -- AuctionScout's
     Sullivan spider broke silently for two weeks when the site changed
     URL schemes; nothing caught it until a manual check. run() here
     logs a row count per town and flags zero-row towns loudly rather
     than silently writing an empty file.
  2. Retry/backoff for transient errors -- added to AuctionScout's
     base.py after a real SSL handshake failure took down a run.
     Government GIS servers are not always reliable infrastructure.
"""

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path

# The target schema every spider normalizes into. Fields a given state's
# source genuinely doesn't provide are set to None -- never guessed at,
# same convention as NH's LAND_USE_STANDARDIZATION ("unrecognized ->
# left as-is, not guessed at").
COMMON_SCHEMA_FIELDS = [
    "property_id",       # spider-assigned stable id: "{state}:{parcel_id}"
    "state",
    "county",
    "municipality",
    "parcel_id",          # the source's own parcel/link/PID identifier
    "address",
    "city",
    "zip",
    "latitude",
    "longitude",
    "acreage",
    "assessed_value",
    "assessed_land_value",
    "assessed_building_value",
    "assessment_year",
    "last_sale_price",
    "last_sale_date",
    "building_sqft",
    "bedrooms",
    "bathrooms",
    "year_built",
    "property_type",
    "source",            # e.g. "CT_OPM_CAMA_2025", "NH_GRANIT_VGSI"
    "source_url",
    "source_date",        # when THIS spider run pulled the data
]


class SpiderError(Exception):
    """Raised for a fetch failure that retries couldn't resolve."""


class StateSpider(ABC):
    """Subclass per state. Must set state_code and implement fetch_town()."""

    state_code: str = None  # e.g. "CT", "NH" -- set by subclass

    max_retries = 3
    retry_backoff_seconds = 2.0

    @abstractmethod
    def fetch_town(self, town: str) -> list[dict]:
        """
        Fetch and normalize every parcel record for one town/municipality.
        Must return a list of dicts, each containing EVERY key in
        COMMON_SCHEMA_FIELDS (use None for fields the source doesn't
        provide -- never omit a key). Each dict's "geometry" is handled
        separately via geometry-in-feature, see _build_feature().
        """
        raise NotImplementedError

    def fetch_town_geometry(self, town: str) -> dict:
        """
        Returns a dict mapping parcel_id -> GeoJSON geometry object for
        the town. Kept separate from fetch_town()'s attribute dict so a
        subclass can fetch geometry and attributes via different calls
        if the source requires it (matches how CT/NH will likely differ:
        CT's query can return geometry inline; NH's join step already
        produces geometry-bearing features).
        Default implementation assumes fetch_town() already embedded a
        "_geometry" key on each record and pops it out here -- override
        if your source needs a genuinely separate fetch.
        """
        raise NotImplementedError

    def _retry(self, fn, *args, **kwargs):
        """Call fn with retries on transient failure. Re-raises the last
        exception (wrapped in SpiderError) if every attempt fails."""
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 -- deliberately broad, retry layer
                last_exc = e
                if attempt < self.max_retries:
                    wait = self.retry_backoff_seconds * attempt
                    print(f"  attempt {attempt}/{self.max_retries} failed ({e}); "
                          f"retrying in {wait:.0f}s")
                    time.sleep(wait)
        raise SpiderError(f"failed after {self.max_retries} attempts: {last_exc}") from last_exc

    def _validate_records(self, records: list[dict], town: str):
        missing_keys = set()
        for r in records:
            missing_keys |= (set(COMMON_SCHEMA_FIELDS) - set(r.keys()))
        if missing_keys:
            raise SpiderError(
                f"{self.state_code} {town}: records missing required schema "
                f"keys: {sorted(missing_keys)} -- fix fetch_town() to always "
                f"include every COMMON_SCHEMA_FIELDS key (None if unavailable)"
            )
        no_parcel_id = sum(1 for r in records if not r.get("parcel_id"))
        no_geometry = sum(1 for r in records if not r.get("_geometry"))
        if no_parcel_id:
            print(f"  WARNING: {no_parcel_id}/{len(records)} records have no parcel_id")
        if no_geometry:
            print(f"  WARNING: {no_geometry}/{len(records)} records have no geometry")

    def _write_geojson(self, records: list[dict], out_path: Path):
        features = []
        for r in records:
            geometry = r.pop("_geometry", None)
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": r,
            })
        fc = {"type": "FeatureCollection", "features": features}
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(fc, f)

    def run(self, towns: list[str], out_dir: str):
        """
        Fetch every town, validate, write one GeoJSON file per town to
        <out_dir>/<state_code>_<town_slug>.geojson. Prints a per-town row
        count -- a silent zero-row town is exactly the AuctionScout
        Sullivan-spider failure mode this is meant to catch early.
        """
        out_dir_path = Path(out_dir)
        summary = []
        for town in towns:
            print(f"[{self.state_code}] fetching {town}...")
            records = self._retry(self.fetch_town, town)
            self._validate_records(records, town)
            town_slug = town.lower().replace(" ", "_")
            out_path = out_dir_path / f"{self.state_code.lower()}_{town_slug}.geojson"
            self._write_geojson(records, out_path)
            print(f"  {len(records)} records -> {out_path}")
            summary.append((town, len(records)))

        zero_row_towns = [t for t, n in summary if n == 0]
        if zero_row_towns:
            print(f"WARNING: zero records for: {zero_row_towns} -- "
                  f"likely a broken query/URL, not genuinely empty towns")
        return summary