"""
VGSI assessed-value scraper — ValueGap

GRANIT gives us boundaries + property type but NOT dollar assessed values
(the state strips those from the public layer). This script fills that gap
by walking a town's Vision Government Solutions (VGSI) parcel record pages
directly, which is what most NH/VT/MA/CT towns use for public assessment
lookup.

TWO-PASS DESIGN:
  Pass 1 (sequential): walk PIDs 1..pid_end. Cheap, doesn't depend on
  address data quality, catches the bulk of a town's normal-range parcels.
  Known limitation: VGSI's internal Pid numbering is NOT one contiguous
  range per town -- an entire newer subdivision can sit at PIDs tens of
  thousands above everything else (confirmed: Lincoln's Crooked
  Mtn/Friendship Ct/South Peak Rd cluster lives at PIDs 102686-103011+,
  vs. the town's main range being a few thousand). No pid_end is "safely
  high enough" to catch this by scanning further -- it would mean walking
  ~100,000 mostly-empty PIDs per town.

  Pass 2 (targeted, automatic): after Pass 1 finishes, compare every
  StreetAddress in the GRANIT parcels geojson against every address Pass 1
  actually found. Anything in GRANIT's list that Pass 1 never matched gets
  looked up directly via VGSI's own address-autocomplete endpoint
  (async.asmx/GetDataAddress -- confirmed live and working, e.g. searching
  "250 S Pea" returns Pid 103011 / "250 SOUTH PEAK ROAD" directly), and its
  full record is fetched and appended to the SAME output CSV. No separate
  supplemental file, no manual append step.

IMPORTANT: this is built from the page's *visible text*, not confirmed HTML
element IDs (I could see the rendered content but not VGSI's raw source).
Run against a few known PIDs first (e.g. Lincoln PID 102691) and compare the
parsed output to the live page before trusting a full crawl -- the regexes
below may need small adjustments once you see the actual HTML.

Usage:
    python vgsi_assessment_scraper.py lincolnnh 1 3000 lincoln_nh.geojson lincoln_assessments.csv
"""

import sys
import csv
import time
import re
import requests
import json
from pathlib import Path
from bs4 import BeautifulSoup

# Shared property-type standardization, used by every state spider (see
# spiders/common/property_types.py's module docstring) -- this used to be
# a LAND_USE_STANDARDIZATION dict local to this file, but that meant NH's
# vocabulary fixes (Lincoln vs. Lebanon) lived nowhere MA or CT could
# reuse them. Added to sys.path the same way nh_spider.py adds this
# file's own directory for granit_parcel_downloader.py etc. -- keeps this
# script runnable standalone from the command line (per this docstring's
# own Usage line), not just importable as part of the spiders package.
_COMMON_DIR = Path(__file__).parent.parent / "common"
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR))
from property_types import standardize_property_type

BASE = "https://gis.vgsi.com/{town}/Parcel.aspx?Pid={pid}"
HEADERS = {"User-Agent": "ValueGap research tool (personal project, low volume)"}

FIELDNAMES = ["pid", "location", "total_market_value", "mblu", "acres", "land_use_desc", "match_source"]

# Same suffix-abbreviation convention as join_parcels_assessments.py's
# normalize_address, duplicated here (rather than imported) so this script
# has no dependency on the join step to determine what counts as "the same
# address" -- Pass 2 needs to compare GRANIT StreetAddress against VGSI
# location text on its own, before any join has happened.
_SUFFIX_MAP = {
    "ROAD": "RD", "STREET": "ST", "LANE": "LN", "DRIVE": "DR",
    "AVENUE": "AVE", "MOUNTAIN": "MTN", "TRAIL": "TRL", "CIRCLE": "CIR",
    "COURT": "CT", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "PLACE": "PL",
}


def normalize_address(raw: str) -> str:
    """Uppercase, strip punctuation, collapse whitespace, abbreviate
    suffixes -- so 'South Peak Road' and 'S PEAK RD' compare equal."""
    if not raw:
        return ""
    s = re.sub(r"[^\w\s]", " ", raw.upper())
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [_SUFFIX_MAP.get(tok, tok) for tok in s.split(" ")]
    return " ".join(tokens)


def standardize_land_use(raw: str | None) -> str | None:
    """Thin alias kept so parse_parcel() below doesn't need to change --
    the real implementation now lives in spiders/common/property_types.py
    and is shared with every other state spider."""
    return standardize_property_type(raw)


def parse_parcel(html: str) -> dict | None:
    """Pull the fields we need out of a VGSI parcel page's visible text."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

    # A PID with no valid parcel typically redirects to a blank/error page --
    # bail out if the page doesn't look like a real record.
    if "Total Market Value" not in text:
        return None

    def grab(label: str, pattern: str = r"\$?([\d,]+)"):
        m = re.search(re.escape(label) + r"\s*\n?\s*" + pattern, text)
        return m.group(1).replace(",", "") if m else None

    # Capture whatever follows the literal "Location" label, rather than
    # guessing at street-suffix patterns -- VGSI cards always use this exact
    # field name, but the value itself varies a lot (private road names,
    # "#LOT" unit suffixes on undeveloped land, etc.) so matching the label
    # is far more reliable than matching the shape of an address.
    location_m = re.search(r"Location\s*\n+\s*(.+?)\s*\n", text)

    # The Land Use section has a "Description" field (e.g. "Single Family",
    # "Residential Land") -- this is the assessor's own plain-English
    # classification, confirmed against PID 3813 (Description: Single
    # Family). Scope the search to start after the "Land Use" heading so we
    # don't accidentally grab an unrelated "Description" label elsewhere on
    # the page.
    land_use_section = text.split("Land Use", 1)
    land_use_desc = None
    if len(land_use_section) > 1:
        desc_m = re.search(r"Description\s*\n+\s*(.+?)\s*\n", land_use_section[1])
        if desc_m:
            land_use_desc = standardize_land_use(desc_m.group(1).strip())
    return {
        "location": location_m.group(1).strip() if location_m else None,
        "total_market_value": grab("Total Market Value"),
        "pid": grab("PID", r"(\d+)"),
        "mblu": grab("Mblu", r"([\d/ ]+)"),
        "land_use_desc": land_use_desc,
        "acres": grab("Size (Acres)", r"([\d.]+)"),
        "raw_text_ok": True,
    }


def find_missing_addresses(found_locations: list[str], granit_geojson_path: str) -> list[str]:
    """
    Compare every non-blank StreetAddress in the GRANIT parcels geojson
    against every location Pass 1 actually found. Returns the original
    (non-normalized) GRANIT address text for anything Pass 1 missed, so
    Pass 2 has real addresses to search VGSI with.
    """
    with open(granit_geojson_path) as f:
        parcels = json.load(f)

    expected_by_key: dict[str, str] = {}
    for feature in parcels["features"]:
        raw = feature.get("properties", {}).get("StreetAddress")
        if not raw or not raw.strip():
            continue  # blank addresses (common land, ROW slivers) -- nothing to search for
        key = normalize_address(raw)
        if key and key not in expected_by_key:
            expected_by_key[key] = raw.strip()

    found_keys = {normalize_address(loc) for loc in found_locations if loc}

    missing_keys = set(expected_by_key) - found_keys
    return [expected_by_key[k] for k in missing_keys]


def scrape_town(town_slug: str, pid_start: int, pid_end: int, granit_geojson_path: str,
                 out_path: str, max_consecutive_misses: int = 300):
    """
    Pass 1: walk PIDs sequentially. VGSI PIDs are dense but not perfectly
    contiguous (demolished/merged parcels leave gaps), so we tolerate gaps
    but bail out after a long consecutive run of misses -- that's a strong
    signal we've run past the top of the town's main PID range (NOT
    necessarily the top of the town's real PID range -- see Pass 2).

    max_consecutive_misses defaults to 300 (raised from an earlier 50) --
    a run of Lincoln, NH showed a legitimate mid-range gap (block 132) that
    a threshold of 50 may have been enough to misinterpret as "end of town",
    stopping the crawl early and silently leaving real parcels unscraped.

    Pass 2: automatically runs after Pass 1 -- see module docstring.
    """
    rows = []
    consecutive_misses = 0
    last_pid_seen = pid_start
    stopped_early = False

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()

        for pid in range(pid_start, pid_end + 1):
            last_pid_seen = pid
            url = BASE.format(town=town_slug, pid=pid)
            try:
                resp = requests.get(url, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                parsed = parse_parcel(resp.text)
            except requests.RequestException as e:
                print(f"  pid {pid}: request failed ({e})")
                parsed = None

            if parsed is None:
                consecutive_misses += 1
                if consecutive_misses >= max_consecutive_misses:
                    stopped_early = True
                    print("=" * 60)
                    print(f"PASS 1 STOPPED EARLY: {max_consecutive_misses} consecutive misses,")
                    print(f"last PID checked was {pid} (requested range was {pid_start}-{pid_end})")
                    print("This is expected/fine -- Pass 2 below will catch real parcels")
                    print("that live outside this sequential range.")
                    print("=" * 60)
                    break
                continue

            consecutive_misses = 0
            row = {
                "pid": parsed["pid"] or pid,
                "location": parsed["location"],
                "total_market_value": parsed["total_market_value"],
                "mblu": parsed["mblu"],
                "acres": parsed["acres"],
                "land_use_desc": parsed["land_use_desc"],
                "match_source": "sequential",
            }
            writer.writerow(row)
            rows.append(row)

            if pid % 100 == 0:
                print(f"  ...at pid {pid}, {len(rows)} parcels captured so far")

            time.sleep(0.5)  # polite pacing against a small town's server

    print("=" * 60)
    if stopped_early:
        print(f"Pass 1 done (stopped early at pid {last_pid_seen} of requested {pid_end}).")
    else:
        print(f"Pass 1 done (completed full range through pid {last_pid_seen}).")
    print(f"Pass 1: {len(rows)} parcels written to {out_path}")
    print("=" * 60)

    # ---- Pass 2: targeted lookup for addresses Pass 1 never found ----
    # Deferred import to avoid a circular import: vgsi_targeted_lookup.py
    # itself does `from vgsi_assessment_scraper import parse_parcel`. If
    # this were a top-level import instead, loading either file first would
    # fail trying to import from the other, which is still mid-loading.
    # By the time scrape_town() actually runs (this function has already
    # been fully defined and this module fully loaded), the cycle is safe.
    from vgsi_targeted_lookup import lookup_and_fetch

    found_locations = [r["location"] for r in rows]
    missing_addresses = find_missing_addresses(found_locations, granit_geojson_path)

    print(f"PASS 2: {len(missing_addresses)} GRANIT addresses have no match from Pass 1 -- "
          f"looking each up directly via VGSI's address search...")
    print("=" * 60)

    targeted_rows = []
    no_match, ambiguous, failed = [], [], []

    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        for i, address in enumerate(missing_addresses, 1):
            parsed, status = lookup_and_fetch(town_slug, address)
            print(f"  [{i}/{len(missing_addresses)}] {address}: {status}"
                  + (f" (pid {parsed['pid']})" if parsed else ""))

            if status == "no_match":
                no_match.append(address)
            elif status.startswith("ambiguous"):
                ambiguous.append(address)
            elif status.startswith("search_failed") or status.startswith("fetch_failed"):
                failed.append(address)

            if parsed:
                row = {
                    "pid": parsed["pid"],
                    "location": parsed["location"],
                    "total_market_value": parsed["total_market_value"],
                    "mblu": parsed["mblu"],
                    "acres": parsed["acres"],
                    "land_use_desc": parsed["land_use_desc"],
                    "match_source": "targeted",
                }
                writer.writerow(row)
                targeted_rows.append(row)

            time.sleep(0.3)  # polite pacing, same as vgsi_targeted_lookup.py

    print("=" * 60)
    print(f"Pass 2 done: {len(targeted_rows)} / {len(missing_addresses)} matched and appended.")
    print(f"  no_match: {len(no_match)}")
    print(f"  ambiguous (took first result -- spot check these): {len(ambiguous)}")
    print(f"  failed (request/parse error): {len(failed)}")
    if ambiguous:
        print("  ambiguous addresses:")
        for a in ambiguous:
            print(f"    {a}")
    if no_match:
        sample = no_match[:20]
        print(f"  sample no_match addresses ({len(no_match)} total):")
        for a in sample:
            print(f"    {a}")
    print("=" * 60)
    print(f"FINAL: {len(rows) + len(targeted_rows)} total parcels written to {out_path} "
          f"({len(rows)} sequential + {len(targeted_rows)} targeted).")
    print("=" * 60)

    return rows + targeted_rows


def main():
    if len(sys.argv) != 6:
        print("Usage: python vgsi_assessment_scraper.py <town_slug> <pid_start> <pid_end> "
              "<granit_parcels.geojson> <output.csv>")
        print("Example: python vgsi_assessment_scraper.py lincolnnh 1 3000 lincoln_nh.geojson "
              "lincoln_assessments.csv")
        sys.exit(1)

    town_slug = sys.argv[1]
    pid_start, pid_end = int(sys.argv[2]), int(sys.argv[3])
    granit_geojson_path = sys.argv[4]
    out_path = sys.argv[5]

    scrape_town(town_slug, pid_start, pid_end, granit_geojson_path, out_path)


if __name__ == "__main__":
    main()