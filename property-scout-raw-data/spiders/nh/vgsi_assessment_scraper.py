"""
VGSI assessed-value scraper — ValueGap

GRANIT gives us boundaries + property type but NOT dollar assessed values
(the state strips those from the public layer). This script fills that gap
by walking a town's Vision Government Solutions (VGSI) parcel record pages
directly, which is what most NH/VT/MA/CT towns use for public assessment
lookup.

BROKEN THEN FIXED (2026-08-25): VGSI redesigned their parcel page layout
at some point between the original build and now -- CONFIRMED via a real
pasted page (Amherst, NH, PID 1, 135 Amherst St #18). This broke every
single page fetch, not just new towns: parse_parcel()'s very first check
gated on finding the literal text "Total Market Value" anywhere on the
page, and that string no longer appears at all under the new layout. The
total assessed value moved to a field simply labeled "Assessment" --
appears once near the top of the page (right after Owner, before PID:
"Assessment\n$442,900"), and again as the "Total" column of a
"Valuation Year | Improvements | Land | Total" table further down
("Current Value" section). Confirmed via the real sample that the FIRST
occurrence of "Assessment" in the page text is immediately followed by
the dollar figure, in the same shape grab() already expects -- so the
fix is a label-name swap, not a restructure of the parsing approach.

The validity gate (deciding "is this a real parcel page, not a blank/
error page") also had to move off "Total Market Value" for the same
reason -- now gates on "PID" being present instead, which is confirmed
present on the real sample and was already being grabbed separately
before this fix (grab("PID", ...)).

NOT YET RE-CONFIRMED against the new layout (the pasted sample didn't
include this far down the page): Mblu, Land Use/Description section,
Size (Acres). Left AS-IS below on the assumption the redesign was
scoped to the assessment-value section specifically (Location/Mblu/
Owner/PID all still matched the OLD assumed positions/labels in the
real sample) -- but this is an assumption, not a confirmation. If
land_use_desc or acres start coming back consistently empty across a
real run post-fix, that's the signal this assumption was wrong and
those need the same treatment total_market_value just got.

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
import re
import requests
import json
import concurrent.futures
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

# SPEEDUP (2026-08-26): the original design was fully serial -- one request,
# wait, time.sleep(0.5), repeat -- which meant a town with pid_end=8000 spent
# 66+ minutes in sleep() alone, on top of per-request latency inflated by
# requests.get() opening a brand-new TCP+TLS connection every single time
# instead of reusing one. A real 40-town run of this pipeline took over 48
# hours and didn't finish.
#
# Fix has two parts, both applied to Pass 1 (scrape_town) and Pass 2
# (vgsi_targeted_lookup.lookup_and_fetch, via a shared session passed in):
#   1. One requests.Session() reused for every request in a town's run,
#      instead of a fresh connection per PID.
#   2. A small bounded thread pool (MAX_WORKERS) instead of one request at
#      a time -- these are I/O-bound waits, exactly where concurrency helps.
#
# MAX_WORKERS deliberately conservative (5) -- VGSI is a small public-sector
# vendor site with no documented rate limit, and this project has already
# been asked to be a "low volume" polite citizen (see HEADERS above). Raise
# this only after watching a real run for 429s/errors at this level first.
MAX_WORKERS = 5

# PIDs are submitted and awaited in chunks of this size (bounded to
# MAX_WORKERS concurrent in flight at once by the executor) so the
# consecutive-miss early-stop logic and the "pid % 100" progress print can
# still be evaluated in strict PID order afterward, same as the old serial
# loop -- concurrency changes WHEN results arrive, not the order they're
# processed in once they're all back.
BATCH_SIZE = 50

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

    # FIXED (this pass): the previous gate checked only for "PID" as
    # visible text, which was correct for the redesigned layout (confirmed
    # via Amherst) but wrong for towns still on VGSI's OLD layout --
    # CONFIRMED via Manchester, PID 29475 ("352 W Haven Rd"): "PID" never
    # appears as visible text on the old layout at all, even though the
    # page is a completely valid, fully populated parcel record (contains
    # "Location", "Total Market Value", and "Assessment"). The old gate
    # was silently discarding every good record from towns like this,
    # which is what inflated Manchester's Pass 2b fallback to 28,132
    # addresses -- most of those were never actually missing from VGSI.
    #
    # New gate accepts either layout: "Location" is present on both, and
    # "Total Market Value" (old) / "Assessment" (new) confirm it's a real
    # populated record and not a blank/error page.
    if "Location" not in text and "Total Market Value" not in text and "Assessment" not in text:
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

    # FIXED (this pass): try the OLD layout's label first ("Total Market
    # Value" -- CONFIRMED present and followed by a dollar figure on
    # Manchester PID 29475), falling back to the NEW layout's first
    # "Assessment" occurrence (CONFIRMED via Amherst) if the old label
    # isn't there. Checking the more specific "Total Market Value" string
    # first avoids any risk of it matching something unintended on a new-
    # layout page that happens to also contain the word "Assessment"
    # elsewhere before the real value.
    total_market_value = grab("Total Market Value") or grab("Assessment")

    # The Land Use section has a "Description" field (e.g. "Single Family",
    # "Residential Land") -- this is the assessor's own plain-English
    # classification, confirmed against PID 3813 (Description: Single
    # Family) UNDER THE OLD LAYOUT. NOT yet re-confirmed against the new
    # layout (see module docstring) -- left as-is on the assumption this
    # section is unchanged, revisit if land_use_desc comes back empty at
    # scale post-fix.
    land_use_section = text.split("Land Use", 1)
    land_use_desc = None
    if len(land_use_section) > 1:
        desc_m = re.search(r"Description\s*\n+\s*(.+?)\s*\n", land_use_section[1])
        if desc_m:
            land_use_desc = standardize_land_use(desc_m.group(1).strip())

    return {
        "location": location_m.group(1).strip() if location_m else None,
        "total_market_value": total_market_value,
        # NOTE: "pid" will legitimately come back None on old-layout pages,
        # since that label simply isn't rendered as visible text there --
        # this is expected, not a bug. Every caller already has a fallback:
        # _scan_pid_range uses `parsed["pid"] or pid` (the PID it already
        # knows from the loop), and vgsi_targeted_lookup._fetch_and_parse
        # overwrites parsed["pid"] = pid from the search result. Nothing
        # downstream depends on this regex succeeding.
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


def _fetch_pid(session: requests.Session, town_slug: str, pid: int):
    """
    Fetch + parse one PID. Returns (pid, parsed_or_None, error_or_None) --
    always returns the pid so results can be re-sorted back into order
    after concurrent.futures.as_completed() returns them out of order.
    """
    url = BASE.format(town=town_slug, pid=pid)
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        parsed = parse_parcel(resp.text)
    except requests.RequestException as e:
        return pid, None, str(e)
    return pid, parsed, None


def _scan_pid_range(session: requests.Session, town_slug: str, writer: csv.DictWriter,
                     pid_start: int, pid_end: int, match_source: str,
                     max_consecutive_misses: int | None, progress_label: str) -> list[dict]:
    """
    Concurrently fetch every PID in [pid_start, pid_end] (MAX_WORKERS at a
    time, batched, over the shared session), writing each real hit to
    `writer` tagged with match_source, and returning the rows written.

    max_consecutive_misses=None scans the FULL range regardless of misses
    -- appropriate for a short, already-located cluster window, where the
    goal is "check every PID in this window" rather than "find where a
    long empty stretch begins." Pass 1 (the wide primary range) still
    wants the early-stop behavior; a cluster sweep (a window this
    function itself sized around a real discovered PID) does not -- it's
    already narrow and already known to contain real parcels.

    This is the same batch/thread-pool logic Pass 1 used before it was
    split out here -- pulled into its own function so the cluster sweep
    (see scrape_town()) can reuse it on arbitrary PID windows instead of
    only the one primary range.
    """
    rows = []
    consecutive_misses = 0
    pid_range = list(range(pid_start, pid_end + 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for batch_start in range(0, len(pid_range), BATCH_SIZE):
            batch = pid_range[batch_start: batch_start + BATCH_SIZE]

            futures = {executor.submit(_fetch_pid, session, town_slug, pid): pid
                       for pid in batch}
            results_by_pid = {}
            for future in concurrent.futures.as_completed(futures):
                pid, parsed, err = future.result()
                if err:
                    print(f"  pid {pid}: request failed ({err})")
                results_by_pid[pid] = parsed

            # Re-walk the batch in real PID order -- as_completed() above
            # returns whichever finished first, not ascending order, but
            # the miss-counting/progress-print logic below depends on it.
            stop = False
            for pid in batch:
                parsed = results_by_pid.get(pid)

                if parsed is None:
                    consecutive_misses += 1
                    if max_consecutive_misses is not None and consecutive_misses >= max_consecutive_misses:
                        stop = True
                        print("=" * 60)
                        print(f"[{progress_label}] STOPPED EARLY: {max_consecutive_misses} consecutive "
                              f"misses, last PID checked was {pid} (requested range was "
                              f"{pid_start}-{pid_end})")
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
                    "match_source": match_source,
                }
                writer.writerow(row)
                rows.append(row)

                if pid % 100 == 0:
                    print(f"  [{progress_label}] ...at pid {pid}, {len(rows)} parcels captured so far")

            if stop:
                break

    return rows


def _group_into_clusters(pids: list[int], gap_threshold: int) -> list[tuple[int, int]]:
    """
    Groups discovered PIDs into (min, max) windows -- two PIDs within
    gap_threshold of each other are treated as belonging to the same
    real subdivision cluster (same underlying assumption as Pass 1's
    max_consecutive_misses: a real cluster's PIDs are dense, not scattered
    lone numbers). A standalone discovered PID with no near neighbor still
    becomes its own single-PID cluster -- cheap to sweep (with padding,
    see CLUSTER_WINDOW_PAD) rather than dropped, since a genuine
    subdivision may still have more members just outside the sample.
    """
    if not pids:
        return []
    ordered = sorted(set(pids))
    clusters = []
    start = prev = ordered[0]
    for pid in ordered[1:]:
        if pid - prev <= gap_threshold:
            prev = pid
            continue
        clusters.append((start, prev))
        start = prev = pid
    clusters.append((start, prev))
    return clusters


# Heuristic constants for cluster discovery/sweep -- tuned against the
# one confirmed real example (Lincoln's Crooked Mtn/Friendship Ct/South
# Peak Rd cluster, PIDs 102686-103011, ~325 PIDs wide) and Lebanon's
# observed 100000-100100 / 100900+ clusters, but NOT yet validated across
# a full 40-town run. If a real run's cluster_sweep match_source rows come
# back suspiciously low relative to how many addresses the sweep was
# supposed to resolve, raise CLUSTER_DISCOVERY_SAMPLE_SIZE first -- it's
# the input to everything else here, and a bad sample can't discover a
# cluster it never got a candidate PID from.
CLUSTER_DISCOVERY_SAMPLE_SIZE = 20  # how many missing addresses to search first, just to locate clusters
CLUSTER_GAP_THRESHOLD = 2000        # PIDs within this distance of each other are treated as one cluster
CLUSTER_WINDOW_PAD = 150            # extra PIDs scanned on each side of a discovered cluster's min/max


def scrape_town(town_slug: str, pid_start: int, pid_end: int, granit_geojson_path: str,
                 out_path: str, max_consecutive_misses: int = 300):
    """
    Pass 1: walk PIDs sequentially across [pid_start, pid_end]. VGSI PIDs
    are dense but not perfectly contiguous (demolished/merged parcels
    leave gaps), so gaps are tolerated, but the scan bails out after a
    long consecutive run of misses -- a strong signal we've run past the
    top of the town's MAIN PID range (not necessarily the top of the
    town's real PID range -- see Pass 2 below).

    SPEEDUP (2026-08-26): fetched BATCH_SIZE at a time via a bounded
    MAX_WORKERS thread pool over one shared requests.Session(), instead of
    one at a time with a blocking time.sleep(0.5) after each. See the
    module-level comment above BASE/HEADERS.

    Pass 2 -- REDESIGNED (2026-08-26): a real run surfaced the actual
    bottleneck, and it wasn't Pass 1 at all. Some towns (confirmed:
    Lebanon) have entire extra subdivisions sitting at PIDs tens of
    thousands above the main range Pass 1 scans (same phenomenon
    documented for Lincoln's Crooked Mtn cluster) -- e.g. Lebanon's
    primary range tops out around pid_end=8000, but real clusters exist
    at ~100000-100100 and again at ~100900+. The ORIGINAL Pass 2 handled
    every address in gaps like that with a full search-then-fetch --
    trying up to a dozen text variants per address (each its own network
    round trip) before even fetching the matched page. For a hundred-plus
    addresses in one tight PID cluster, that's enormously more requests
    than the cluster actually needs.

    New Pass 2 has two stages:
      2a. CLUSTER SWEEP -- search only a small SAMPLE of the missing
          addresses (CLUSTER_DISCOVERY_SAMPLE_SIZE) to discover roughly
          where in PID-space the rest of the missing addresses live, then
          SEQUENTIALLY SCAN a padded window around each discovered PID
          cluster using the same fast concurrent batch-fetch Pass 1 uses
          (_scan_pid_range). One request per PID in the window, no
          per-address searching -- and it picks up every real parcel in
          that window, not just the ones that happened to be in the
          sample.
      2b. FALLBACK -- whatever's still missing after the sweep (true
          one-offs; addresses whose PID isn't near any discovered
          cluster) goes through the original one-by-one address-search
          path, same as before, just on a hopefully much smaller list.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    all_rows: list[dict] = []

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()

        # ---- Pass 1 ----
        pass1_rows = _scan_pid_range(
            session, town_slug, writer, pid_start, pid_end,
            match_source="sequential", max_consecutive_misses=max_consecutive_misses,
            progress_label=f"Pass 1, pid {pid_start}-{pid_end}",
        )
        all_rows.extend(pass1_rows)

        print("=" * 60)
        print(f"Pass 1 done: {len(pass1_rows)} parcels written to {out_path}")
        print("=" * 60)

        # ---- Pass 2a: cluster discovery + sweep ----
        # Deferred import to avoid a circular import: vgsi_targeted_lookup.py
        # itself does `from vgsi_assessment_scraper import parse_parcel`. By
        # the time this line runs, this module is already fully loaded, so
        # the cycle is safe -- see the original module docstring's note on
        # this same pattern.
        from vgsi_targeted_lookup import lookup_and_fetch

        found_locations = [r["location"] for r in all_rows]
        missing_addresses = find_missing_addresses(found_locations, granit_geojson_path)

        print(f"PASS 2a: {len(missing_addresses)} GRANIT addresses have no match from Pass 1 -- "
              f"sampling up to {CLUSTER_DISCOVERY_SAMPLE_SIZE} to discover any out-of-range "
              f"PID clusters...")
        print("=" * 60)

        if missing_addresses:
            discovery_sample = missing_addresses[:CLUSTER_DISCOVERY_SAMPLE_SIZE]
            discovered_pids = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(lookup_and_fetch, town_slug, address, session): address
                    for address in discovery_sample
                }
                for future in concurrent.futures.as_completed(futures):
                    parsed, status = future.result()
                    if parsed and parsed.get("pid"):
                        discovered_pids.append(int(parsed["pid"]))

            clusters = _group_into_clusters(discovered_pids, CLUSTER_GAP_THRESHOLD)
            if clusters:
                print(f"  discovered {len(clusters)} candidate cluster(s) from "
                      f"{len(discovery_sample)} sampled address(es): {clusters}")
                for lo, hi in clusters:
                    window_lo = max(1, lo - CLUSTER_WINDOW_PAD)
                    window_hi = hi + CLUSTER_WINDOW_PAD
                    print(f"  sweeping cluster window pid {window_lo}-{window_hi}...")
                    swept = _scan_pid_range(
                        session, town_slug, writer, window_lo, window_hi,
                        match_source="cluster_sweep", max_consecutive_misses=None,
                        progress_label=f"cluster {window_lo}-{window_hi}",
                    )
                    all_rows.extend(swept)
                    print(f"  cluster pid {window_lo}-{window_hi}: {len(swept)} parcels captured")
            else:
                print("  no candidate clusters discovered from the sample -- "
                      "every missing address is likely a genuine one-off.")
        print("=" * 60)

        # ---- Pass 2b: fallback address search for whatever's still missing ----
        found_locations = [r["location"] for r in all_rows]
        still_missing = find_missing_addresses(found_locations, granit_geojson_path)

        print(f"PASS 2b: {len(still_missing)} GRANIT addresses still unmatched after the cluster "
              f"sweep -- looking each up directly via VGSI's address search...")
        print("=" * 60)

        targeted_rows = []
        no_match, ambiguous, failed = [], [], []

        # SPEEDUP: same session + MAX_WORKERS pool, instead of one address
        # at a time with a blocking time.sleep(0.3). Order of the printed
        # [i/n] lines is no longer guaranteed to match still_missing's
        # original order (results print as they complete), but every
        # address is still looked up exactly once and every row still gets
        # written -- only the console ordering changed, not the output.
        if still_missing:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(lookup_and_fetch, town_slug, address, session): address
                    for address in still_missing
                }
                for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    address = futures[future]
                    parsed, status = future.result()
                    print(f"  [{i}/{len(still_missing)}] {address}: {status}"
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

    all_rows.extend(targeted_rows)

    print("=" * 60)
    print(f"Pass 2b done: {len(targeted_rows)} / {len(still_missing)} matched and appended.")
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
    print(f"FINAL: {len(all_rows)} total parcels written to {out_path}.")
    print("=" * 60)

    return all_rows


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