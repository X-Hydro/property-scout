"""
VGSI assessed-value scraper — ValueGap

GRANIT gives us boundaries + property type but NOT dollar assessed values
(the state strips those from the public layer). This script fills that gap
by walking a town's Vision Government Solutions (VGSI) parcel record pages
directly, which is what most NH/VT/MA/CT towns use for public assessment
lookup.

IMPORTANT: this is built from the page's *visible text*, not confirmed HTML
element IDs (I could see the rendered content but not VGSI's raw source).
Run against a few known PIDs first (e.g. Lincoln PID 102691) and compare the
parsed output to the live page before trusting a full crawl -- the regexes
below may need small adjustments once you see the actual HTML.

Usage:
    python vgsi_assessment_scraper.py lincolnnh 1 5000 lincoln_assessments.csv
"""

import sys
import csv
import time
import re
import requests
from bs4 import BeautifulSoup

BASE = "https://gis.vgsi.com/{town}/Parcel.aspx?Pid={pid}"
HEADERS = {"User-Agent": "ValueGap research tool (personal project, low volume)"}


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
            land_use_desc = desc_m.group(1).strip()
    return {
        "location": location_m.group(1).strip() if location_m else None,
        "total_market_value": grab("Total Market Value"),
        "pid": grab("PID", r"(\d+)"),
        "mblu": grab("Mblu", r"([\d/ ]+)"),
        "land_use_desc": land_use_desc,
        "acres": grab("Size (Acres)", r"([\d.]+)"),
        "raw_text_ok": True,
    }


def scrape_town(town_slug: str, pid_start: int, pid_end: int, out_path: str, max_consecutive_misses: int = 300):
    """
    Walk PIDs sequentially. VGSI PIDs are dense but not perfectly contiguous
    (demolished/merged parcels leave gaps), so we tolerate gaps but bail out
    after a long consecutive run of misses -- that's a strong signal we've
    run past the top of the town's real PID range.

    max_consecutive_misses defaults to 300 (raised from an earlier 50) --
    a run of Lincoln, NH showed a legitimate mid-range gap (block 132) that
    a threshold of 50 may have been enough to misinterpret as "end of town",
    stopping the crawl early and silently leaving real parcels unscraped.
    """
    rows = []
    consecutive_misses = 0
    last_pid_seen = pid_start
    stopped_early = False

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "pid", "location", "total_market_value", "mblu", "acres", "land_use_desc"
        ])
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
                    print(f"STOPPED EARLY: {max_consecutive_misses} consecutive misses,")
                    print(f"last PID checked was {pid} (requested range was {pid_start}-{pid_end})")
                    print("If this is well short of pid_end, real parcels may be unscraped.")
                    print("=" * 60)
                    break
                continue

            consecutive_misses = 0
            writer.writerow({
                "pid": parsed["pid"] or pid,
                "location": parsed["location"],
                "total_market_value": parsed["total_market_value"],
                "mblu": parsed["mblu"],
                "acres": parsed["acres"],
                "land_use_desc": parsed["land_use_desc"],
            })
            rows.append(parsed)

            if pid % 100 == 0:
                print(f"  ...at pid {pid}, {len(rows)} parcels captured so far")

            time.sleep(0.5)  # polite pacing against a small town's server

    print("=" * 60)
    if stopped_early:
        print(f"Done (stopped early at pid {last_pid_seen} of requested {pid_end}).")
    else:
        print(f"Done (completed full range through pid {last_pid_seen}).")
    print(f"{len(rows)} parcels written to {out_path}")
    print("=" * 60)


def main():
    if len(sys.argv) != 5:
        print("Usage: python vgsi_assessment_scraper.py <town_slug> <pid_start> <pid_end> <output.csv>")
        print("Example: python vgsi_assessment_scraper.py lincolnnh 1 3000 lincoln_assessments.csv")
        sys.exit(1)

    town_slug = sys.argv[1]
    pid_start, pid_end = int(sys.argv[2]), int(sys.argv[3])
    out_path = sys.argv[4]

    scrape_town(town_slug, pid_start, pid_end, out_path)


if __name__ == "__main__":
    main()