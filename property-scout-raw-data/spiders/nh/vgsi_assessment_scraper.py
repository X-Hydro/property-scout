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
import os
import re
import time
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

    # FIXED 2026-08-30: the 2026-08-25 fix gated validity on "PID" being
    # present as visible text, which is correct for the redesigned layout
    # (confirmed via Amherst) but WRONG for towns still on VGSI's OLD
    # layout -- CONFIRMED via Manchester, PID 29475 ("352 W Haven Rd"):
    # "PID" never appears as visible text there at all, even though the
    # page is a completely valid, fully populated record. This was
    # silently discarding every real record from any town still on the
    # old layout (Manchester alone: ~28,000 GRANIT addresses wrongly
    # fell through to the slow Pass 2b fallback because of this).
    #
    # New gate accepts either layout: "Location" is present on both
    # layouts; "Total Market Value" (old) / "Assessment" (new) confirms
    # it's a real populated record and not a blank/error page.
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
        # FIXED 2026-08-30: try the OLD layout's label first ("Total
        # Market Value" -- CONFIRMED present and followed by a dollar
        # figure on Manchester PID 29475), falling back to the NEW
        # layout's first "Assessment" occurrence (CONFIRMED via Amherst,
        # right after Owner, before PID) if the old label isn't present.
        # Checking the more specific "Total Market Value" string first
        # avoids any risk of it matching something unintended on a
        # new-layout page that happens to also contain "Assessment"
        # elsewhere before the real value.
        "total_market_value": grab("Total Market Value") or grab("Assessment"),
        # NOTE: legitimately comes back None on old-layout pages, since
        # that label simply isn't rendered as visible text there -- this
        # is expected, not a bug. Both callers already fall back to the
        # PID they already know: _scan_pid_range uses `parsed["pid"] or
        # pid` (the loop's own PID), and vgsi_targeted_lookup's
        # _fetch_and_parse overwrites parsed["pid"] = pid from the
        # search result. Nothing downstream depends on this succeeding.
        "pid": grab("PID", r"(\d+)"),
        "mblu": grab("Mblu", r"([\d/ ]+)"),
        "land_use_desc": land_use_desc,
        "acres": grab("Size (Acres)", r"([\d.]+)"),
        "raw_text_ok": True,
    }


def probe_layout(town_slug: str,
                  probe_pids: tuple[int, ...] = (1, 2, 3, 5, 10)) -> bool:
    """
    ADDED 2026-08-30: cheap pre-flight check, meant to be called before
    committing to a full scrape_town() run. Fetches a handful of low PIDs
    and confirms at least one parses successfully. Both parse_parcel()
    layout variants are now handled (see its docstring), so this mainly
    catches a THIRD, not-yet-seen layout, a wrong town_slug, or the site
    being unreachable -- cases where the whole run is doomed regardless of
    which parser branch runs. Costs ~5 requests instead of discovering the
    problem 28,000 requests into Pass 2b.

    Returns True if at least one probe PID parses; False otherwise (caller
    should abort/warn rather than proceed).
    """
    for pid in probe_pids:
        _, parsed, _ = _fetch_pid(town_slug, pid)
        if parsed:
            return True
    return False


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


import threading

_thread_local = threading.local()


def _get_thread_session() -> requests.Session:
    """
    ADDED 2026-08-30: one requests.Session per worker thread, not one
    shared across all of them.

    A single shared Session()'s connection pool is not fully safe under
    concurrent use against a stateful backend the way you'd want -- under
    sustained concurrent load, requests can end up serialized behind each
    other on shared connection/session state. That matches Newington's
    symptom exactly: fine for ~500 requests, then a specific point where
    latency degrades and NEVER recovers even after a 60s pause (a pause
    clears genuine server-side rate limiting, but not a backed-up shared
    client-side connection state). This is also the documented root cause
    of a near-identical failure mode this project hit before (see prior
    session notes: ASP.NET session-lock contention under a shared
    Session() causing HTTP 500 storms) -- this is the fix for that class
    of problem, just not yet applied to this file until now.
    """
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update(HEADERS)
    return _thread_local.session


class VGSIRequestStorm(Exception):
    """
    ADDED 2026-08-30: raised when a sustained burst of request errors
    indicates the server (or our connection to it) is in a genuinely bad
    state -- not scattered transient noise. CONFIRMED via a real Newington
    run: isolated timeouts at PID ~515 progressively thickened until PIDs
    588-600 were failing almost universally, with retries (3 attempts
    each) not helping at all -- because retries add MORE requests during
    exactly the window the server is struggling, which is the wrong
    response to sustained degradation as opposed to one-off blips.

    Per this project's own principle (see spiders/nh module docs):
    aborting loudly on a request storm is preferable to grinding through
    a doomed range and producing quietly-incomplete data with inflated
    "missing" counts.
    """
    pass


# A request error (timeout, connection reset, etc.) is tracked separately
# from a genuine "no such parcel" miss -- conflating the two (as the
# original consecutive_misses counter did) means a run of network errors
# looks identical to a run past the real top of a town's PID range, which
# can trigger Pass 1's early-stop for the wrong reason.
STORM_WINDOW = 30            # look at the last N fetch attempts (across the whole scan, not per-batch)
STORM_ERROR_THRESHOLD = 0.5  # if >=50% of the last STORM_WINDOW attempts errored, pause and reassess
STORM_COOLDOWN_SECONDS = 60  # how long to pause before trying to resume after tripping
STORM_MAX_COOLDOWNS = 2      # if the error rate is still bad after this many cooldowns, give up and raise


def _fetch_pid(town_slug: str, pid: int, max_attempts: int = 3):
    """
    Fetch + parse one PID. Returns (pid, parsed_or_None, error_or_None) --
    always returns the pid so results can be re-sorted back into order
    after concurrent.futures.as_completed() returns them out of order.

    Uses a per-thread session (see _get_thread_session) rather than a
    session passed in from the caller -- when this runs inside a
    ThreadPoolExecutor, each worker thread gets its own connection, no
    shared state across concurrent requests.

    Retries transient errors with backoff AND an escalating timeout --
    CONFIRMED via two separate Newington runs that the same narrow PID
    band (roughly 500-600+) times out and, notably, does NOT recover
    even after a storm cooldown -- consistent with a shared-session
    connection issue (see _get_thread_session) rather than either slow
    server-side rendering or simple rate limiting, both of which a pause
    should have cleared.
    """
    session = _get_thread_session()
    url = BASE.format(town=town_slug, pid=pid)
    last_err = None
    for attempt in range(1, max_attempts + 1):
        timeout = 15 * attempt  # 15s, 30s, 45s
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            parsed = parse_parcel(resp.text)
            return pid, parsed, None
        except requests.RequestException as e:
            last_err = str(e)
            if attempt < max_attempts:
                time.sleep(0.5 * attempt)  # 0.5s, then 1.0s
    return pid, None, f"{last_err} (after {max_attempts} attempts, up to {15 * max_attempts}s timeout)"


def _scan_pid_range(town_slug: str, writer: csv.DictWriter,
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

    FIXED 2026-08-30 (two related issues, both surfaced by a real
    Newington run):

    1. consecutive_misses used to increment on ANY parsed-is-None result,
       whether that meant "genuinely no parcel at this PID" or "the
       request errored out." A run of network errors therefore looked
       identical to walking past the real top of a town's PID range, and
       could trigger Pass 1's early-stop for entirely the wrong reason.
       Request errors are now tracked separately (see failed_pids) and no
       longer touch consecutive_misses at all.

    2. No mechanism previously existed to notice that errors were
       CLUSTERING -- CONFIRMED via Newington: isolated timeouts around
       PID 515 progressively thickened into near-total failure by PID
       590-600. Per-request retry (see _fetch_pid) doesn't help this
       shape of problem; it just adds more requests during exactly the
       window the server's struggling. See VGSIRequestStorm's docstring.
       A rolling window now tracks the recent error RATE across the whole
       scan; if it spikes, the scan pauses for STORM_COOLDOWN_SECONDS and
       resets the window. If the rate is still bad after
       STORM_MAX_COOLDOWNS attempts, it raises VGSIRequestStorm rather
       than continuing to grind through what's likely a doomed range.
    """
    rows = []
    failed_pids = []  # PIDs that errored (not genuine misses) -- for a possible later retry pass
    consecutive_misses = 0
    recent_error_flags: list[bool] = []  # rolling window, oldest at index 0
    cooldowns_used = 0
    pid_range = list(range(pid_start, pid_end + 1))
    last_pid_checked = pid_start - 1  # ADDED for history tracking -- see this function's return value

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for batch_start in range(0, len(pid_range), BATCH_SIZE):
            batch = pid_range[batch_start: batch_start + BATCH_SIZE]

            futures = {executor.submit(_fetch_pid, town_slug, pid): pid
                       for pid in batch}
            results_by_pid = {}
            for future in concurrent.futures.as_completed(futures):
                pid, parsed, err = future.result()
                if err:
                    print(f"  pid {pid}: request failed ({err})")
                    failed_pids.append(pid)
                results_by_pid[pid] = (parsed, err)

            # Re-walk the batch in real PID order -- as_completed() above
            # returns whichever finished first, not ascending order, but
            # the miss-counting/progress-print logic below depends on it.
            stop = False
            for pid in batch:
                parsed, err = results_by_pid.get(pid, (None, None))
                last_pid_checked = pid  # ADDED for history tracking, updated regardless of hit/miss/error

                recent_error_flags.append(bool(err))
                if len(recent_error_flags) > STORM_WINDOW:
                    recent_error_flags.pop(0)

                if len(recent_error_flags) == STORM_WINDOW:
                    error_rate = sum(recent_error_flags) / STORM_WINDOW
                    if error_rate >= STORM_ERROR_THRESHOLD:
                        if cooldowns_used >= STORM_MAX_COOLDOWNS:
                            raise VGSIRequestStorm(
                                f"[{progress_label}] error rate stayed at/above "
                                f"{STORM_ERROR_THRESHOLD:.0%} over the last {STORM_WINDOW} "
                                f"requests even after {cooldowns_used} cooldown(s) of "
                                f"{STORM_COOLDOWN_SECONDS}s each, last PID checked was {pid}. "
                                f"This looks like sustained throttling/blocking, not transient "
                                f"noise -- aborting rather than grinding through the rest of "
                                f"{pid_start}-{pid_end} producing unreliable data."
                            )
                        print("=" * 60)
                        print(f"[{progress_label}] STORM DETECTED: {error_rate:.0%} error rate "
                              f"over the last {STORM_WINDOW} requests (last PID {pid}). "
                              f"Pausing {STORM_COOLDOWN_SECONDS}s before resuming "
                              f"(cooldown {cooldowns_used + 1}/{STORM_MAX_COOLDOWNS})...")
                        print("=" * 60)
                        time.sleep(STORM_COOLDOWN_SECONDS)
                        recent_error_flags = []
                        cooldowns_used += 1
                        # NOTE: the rest of this batch was already fetched
                        # (the whole batch is submitted concurrently before
                        # this per-PID loop runs) -- `continue` here uses
                        # those already-fetched results instead of
                        # discarding them, no wasted requests.
                        continue

                if err:
                    continue  # request error -- already logged/tracked above, not a genuine miss

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

    if failed_pids:
        print(f"  [{progress_label}] {len(failed_pids)} PID(s) errored out after retries and were "
              f"NOT counted as misses -- true status unknown, consider a follow-up pass: "
              f"{failed_pids[:20]}{'...' if len(failed_pids) > 20 else ''}")

    # CHANGED for history tracking: now returns (rows, last_pid_checked) instead of
    # just rows -- last_pid_checked is the boundary of what was actually scanned
    # (whether the range completed or an early-stop/consecutive-miss triggered),
    # which is what gets persisted as sequenced_range.high / a cluster window's
    # hi for a future run's history replay. All call sites updated accordingly.
    return rows, last_pid_checked


def _group_into_clusters(pids: list[int], gap_threshold: int, max_span: int = 1500) -> list[tuple[int, int]]:
    """
    Groups discovered PIDs into (min, max) windows -- two PIDs within
    gap_threshold of each other are treated as belonging to the same
    real subdivision cluster (same underlying assumption as Pass 1's
    max_consecutive_misses: a real cluster's PIDs are dense, not scattered
    lone numbers). A standalone discovered PID with no near neighbor still
    becomes its own single-PID cluster -- cheap to sweep (with padding,
    see CLUSTER_WINDOW_PAD) rather than dropped, since a genuine
    subdivision may still have more members just outside the sample.

    FIXED 2026-08-30: previously only checked the gap to the immediately
    PRECEDING pid, which allows unbounded transitive chaining -- CONFIRMED
    via a real run that produced a single cluster spanning PIDs
    99994-104493 (4,499 wide) even with gap_threshold=2000, because every
    individual link in the chain happened to be under 2000 even though the
    cumulative span was enormous. Both confirmed real clusters this was
    tuned against (Lincoln: 325 wide, Lebanon: ~100-200 wide) are well
    under max_span=1500 -- anything wider than that is far more likely to
    be an accidental chain (or a bad/ambiguous address match pulling in an
    unrelated PID) than one real dense subdivision, and sweeping it wastes
    hours checking thousands of speculative PIDs that mostly don't exist.
    """
    if not pids:
        return []
    ordered = sorted(set(pids))
    clusters = []
    start = prev = ordered[0]
    for pid in ordered[1:]:
        if pid - prev <= gap_threshold and pid - start <= max_span:
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

# ADDED: separate, much tighter gap threshold used ONLY when re-deriving
# cluster windows from a previous run's CSV (_load_pid_history_from_csv),
# as opposed to CLUSTER_GAP_THRESHOLD's use in LIVE discovery
# (_group_into_clusters called on a small SAMPLE of freshly-searched
# addresses, scrape_town's Pass 2a). Those are different situations:
# live discovery only has a handful of sampled points and has to guess
# generously at a whole cluster's shape from them, so a wide 2000-PID
# tolerance is the right call there. CSV reconstruction, by contrast,
# already has EVERY real hit PID from last run -- no sampling risk to
# guard against -- so a wide tolerance just merges separate subdivisions
# that happen to be within 2000 PIDs of each other into one bloated
# window. CONFIRMED via a real Strafford run: reconstructing with
# CLUSTER_GAP_THRESHOLD=2000 produced two windows that replayed 1801 and
# 1719 PIDs for only 165 and 114 real hits respectively (>90% wasted
# requests, ~3200 PIDs fetched for nothing). A much tighter threshold
# here still correctly bounds one real cluster (built from its actual
# hits, not a sample), while no longer bridging across genuinely
# separate ones.
CLUSTER_RECONSTRUCT_GAP_THRESHOLD = 300

# ADDED: density threshold for deciding HOW to replay a reconstructed
# cluster. A cluster sweep -- whether triggered by fresh Pass 2a discovery
# or replayed from history -- costs the SAME number of requests either
# way: one per PID in the window. History replay only ever saves the
# DISCOVERY step that precedes a fresh sweep (a handful of address-search
# requests), never the sweep itself. That means replaying a SPARSE
# cluster as a padded window is nearly pure overhead -- CONFIRMED via a
# real Strafford run where three reconstructed windows replayed 1801,
# 1791, and 323 PIDs for only 165, 129, and 72 real hits (9%, 7%, 22%
# density) -- 3,915 requests for 366 real parcels.
#
# Below this density, a cluster's KNOWN hit PIDs are folded into
# targeted_pids instead of kept as a window -- replayed as an exact list
# (_fetch_pid_list with no padding/gaps), same as any other targeted PID.
# That turns e.g. the 1801-wide/165-hit case into a 165-request direct
# fetch, an ~91% cut for that cluster specifically. The trade-off: a
# BRAND NEW parcel added inside that cluster's dead space between runs
# won't be caught by this direct-list replay -- but it's still caught the
# normal way the very first time its GRANIT address shows up as
# unmatched (Pass 2a/2b), same as any other new address in town; the only
# cost is that one occurrence goes through fresh discovery once, not that
# it's missed forever. At/above this density, a cluster is kept as a
# padded window as before, since there's little sparse dead space to cut
# and windowing still catches in-fill growth for free on every replay.
CLUSTER_REPLAY_DENSITY_THRESHOLD = 0.3


def _range_fully_covered(lo: int, hi: int, covered: list[tuple[int, int]]) -> bool:
    """
    True if every PID in [lo, hi] falls within a SINGLE already-scanned
    range in `covered`. Deliberately doesn't try to stitch together
    partial coverage from multiple adjacent ranges -- exact single-range
    containment is enough for what this guards against (see
    scrape_town's use in Pass 2a): a cluster window landing entirely
    inside a range the sequenced replay/Pass 1 or growth probe already
    scanned this run.

    ADDED after a real Manchester run: once the growth probe (see
    GROWTH_PROBE_WIDTH's docstring) could scan much wider ranges, Pass 2a
    started rediscovering and re-sweeping windows the growth probe had
    JUST finished scanning moments earlier -- CONFIRMED: a 1,738-request
    cluster sweep of 28081-29865 was entirely redundant with a growth
    probe scan of 21807-30793 that had already covered every PID in it.
    The dedup writer kept the output CSV clean, but the network requests
    were still made twice. Root cause: those addresses stay on Pass 2a's
    "missing" list even after their PID is scanned, because GRANIT's
    StreetAddress field for them is road-name-only (e.g. "STRAW RD", not
    "497 STRAW RD") -- find_missing_addresses can't text-match that to
    anything, regardless of whether the PID itself was already fetched.
    """
    return any(c_lo <= lo and hi <= c_hi for c_lo, c_hi in covered)


def _group_pids_with_members(pids: list[int], gap_threshold: int,
                              max_span: int = 1500) -> list[list[int]]:
    """
    Same grouping logic as _group_into_clusters, but returns each group's
    actual member PIDs (not just its (lo, hi) bounds) -- needed to
    compute per-cluster hit density in _load_pid_history_from_csv, which
    _group_into_clusters' (lo, hi)-only return can't support.
    """
    if not pids:
        return []
    ordered = sorted(set(pids))
    groups = [[ordered[0]]]
    for pid in ordered[1:]:
        if pid - groups[-1][-1] <= gap_threshold and pid - groups[-1][0] <= max_span:
            groups[-1].append(pid)
        else:
            groups.append([pid])
    return groups

# ADDED: PID-history replay support. A monthly re-run of a town whose PID
# layout is already known (from a prior run's history file) shouldn't have
# to re-walk a blind 1..pid_end range or re-run per-address VGSI searches
# for parcels we already know the PID of -- it can just re-fetch each known
# PID directly (one request each, no address-search variants). See
# _fetch_pid_list / _load_pid_history / _save_pid_history / scrape_town.
GROWTH_PROBE_WIDTH = 2000       # how far past the last known sequenced-range high to probe for new growth
GROWTH_PROBE_MAX_MISSES = 100   # smaller than Pass 1's max_consecutive_misses -- just confirming growth
                                 # stopped again, not discovering a whole fresh range from scratch


def _fetch_pid_list(town_slug: str, writer: csv.DictWriter, pids, match_source: str,
                     progress_label: str) -> tuple[list[dict], list[int]]:
    """
    Fetch a specific, possibly-noncontiguous collection of PIDs concurrently
    (accepts any iterable of ints, e.g. a range() or an explicit list).

    Unlike _scan_pid_range, a parsed=None result here is NOT a signal to
    stop -- these PIDs are being REPLAYED from a previous run's history
    (sequenced_range / cluster_windows / targeted_pids), so the large
    majority are expected to still hit. A miss just means that specific
    parcel was merged, demolished, or renumbered since the last run --
    normal month-to-month turnover, not a sign we've walked off the end of
    the town's real PID range. There is no "end" to walk off of here: the
    input is exactly the set of PIDs we already know about.

    Keeps the same request-storm circuit breaker _scan_pid_range uses,
    since a sustained burst of connection errors is still a sign something
    is wrong with the server/connection regardless of why this particular
    PID list was assembled.

    Returns (rows_written, missing_pids) -- missing_pids is every PID in
    the input that did NOT resolve to a real parcel this time (miss,
    error, or dropped during a storm cooldown), so the caller can report
    how much churn happened since the last run (e.g. "14 of 812 replayed
    PIDs no longer resolve") and so scrape_town can decide what to keep in
    the rewritten history file.
    """
    rows: list[dict] = []
    missing_pids: list[int] = []
    recent_error_flags: list[bool] = []
    cooldowns_used = 0
    pids = list(pids)
    total = len(pids)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for batch_start in range(0, total, BATCH_SIZE):
            batch = pids[batch_start: batch_start + BATCH_SIZE]
            futures = {executor.submit(_fetch_pid, town_slug, pid): pid for pid in batch}
            results_by_pid = {}
            for future in concurrent.futures.as_completed(futures):
                pid, parsed, err = future.result()
                if err:
                    print(f"  [{progress_label}] pid {pid}: request failed ({err})")
                results_by_pid[pid] = (parsed, err)

            for pid in batch:
                parsed, err = results_by_pid.get(pid, (None, None))

                recent_error_flags.append(bool(err))
                if len(recent_error_flags) > STORM_WINDOW:
                    recent_error_flags.pop(0)
                if len(recent_error_flags) == STORM_WINDOW:
                    error_rate = sum(recent_error_flags) / STORM_WINDOW
                    if error_rate >= STORM_ERROR_THRESHOLD:
                        if cooldowns_used >= STORM_MAX_COOLDOWNS:
                            raise VGSIRequestStorm(
                                f"[{progress_label}] error rate stayed at/above "
                                f"{STORM_ERROR_THRESHOLD:.0%} over the last {STORM_WINDOW} "
                                f"requests even after {cooldowns_used} cooldown(s), last pid "
                                f"checked was {pid}. Aborting replay rather than grinding "
                                f"through the rest of the known-pid list producing unreliable "
                                f"data."
                            )
                        print("=" * 60)
                        print(f"[{progress_label}] STORM DETECTED: {error_rate:.0%} error rate "
                              f"over the last {STORM_WINDOW} requests (last pid {pid}). "
                              f"Pausing {STORM_COOLDOWN_SECONDS}s before resuming "
                              f"(cooldown {cooldowns_used + 1}/{STORM_MAX_COOLDOWNS})...")
                        print("=" * 60)
                        time.sleep(STORM_COOLDOWN_SECONDS)
                        recent_error_flags = []
                        cooldowns_used += 1
                        continue

                if err:
                    missing_pids.append(pid)
                    continue

                if parsed is None:
                    missing_pids.append(pid)
                    continue

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

            checked_so_far = batch_start + len(batch)
            if (batch_start // BATCH_SIZE) % 10 == 0 or checked_so_far >= total:
                print(f"  [{progress_label}] ...{checked_so_far}/{total} pids checked, "
                      f"{len(rows)} parcels captured so far")

    return rows, missing_pids


class _DedupWriter:
    """
    Thin wrapper around a csv.DictWriter that skips a row if its 'pid' was
    already written earlier in THIS run.

    ADDED alongside CSV-derived history: once cluster windows can be
    re-discovered every run (via _load_pid_history_from_csv below) rather
    than persisted verbatim, a replayed window and a freshly (re)discovered
    window for the same real cluster can end up overlapping -- both get
    scanned, and without this guard the same PID would get written to the
    output CSV twice in one run. Duplicate rows would then also inflate
    the *next* run's cluster-window reconstruction (though not by much,
    since _group_into_clusters dedupes via set() -- this is about keeping
    the CSV itself clean for whatever reads it downstream, e.g.
    join_parcels_assessments.py).

    writerow() returns True if the row was actually written, False if it
    was a duplicate and skipped -- callers use this to decide whether to
    count the row (e.g. append it to their own `rows` list).
    """
    def __init__(self, inner_writer: csv.DictWriter):
        self._inner = inner_writer
        self.seen_pids: set[str] = set()

    def writerow(self, row: dict) -> bool:
        pid = str(row.get("pid"))
        if pid in self.seen_pids:
            return False
        self.seen_pids.add(pid)
        self._inner.writerow(row)
        return True


def _load_pid_history_from_csv(csv_path: str, pid_start: int) -> dict | None:
    """
    Reconstruct the same 'known PID index' a separate history file would
    hold, directly from a PREVIOUS run's output CSV -- no extra file to
    manage, nothing that can drift out of sync with what was actually
    scraped. This is what makes rerunning the exact same command (same
    out_path) automatically fast the second time: the CSV this run is
    about to overwrite already records, in its match_source column,
    which PIDs came from the sequential scan, which came from a cluster
    sweep, and which came from a targeted address search.

    Must be called BEFORE out_path could be overwritten by this run's
    result (see scrape_town's atomic-write handling) -- reads the file as
    it was left by the LAST successful run.

    Returns None if csv_path doesn't exist yet (first-ever run for this
    town) or contains no recognized rows.
    """
    p = Path(csv_path)
    if not p.exists():
        return None

    sequential_pids, cluster_pids, targeted_pids = [], [], []
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            pid_str = (row.get("pid") or "").strip()
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            source = row.get("match_source", "")
            if source in ("sequential", "sequential_replay"):
                sequential_pids.append(pid)
            elif source in ("cluster_sweep", "cluster_replay"):
                cluster_pids.append(pid)
            elif source in ("targeted", "targeted_replay"):
                targeted_pids.append(pid)

    if not (sequential_pids or cluster_pids or targeted_pids):
        return None  # empty or unrecognized CSV -- treat as no history

    # sequenced_range.low is just this run's own pid_start (the CLI arg,
    # same every run for a given town) -- nothing to reconstruct there.
    # .high is the furthest PID the sequential scan (including any past
    # growth probes, which are tagged match_source="sequential" too)
    # actually confirmed a hit at. It may be a bit lower than the true
    # last-PID-checked from the original scan (which also isn't stored
    # here) -- fine, since the growth probe below just re-covers that
    # small gap along with looking for further growth, at no extra cost.
    sequenced_high = max(sequential_pids) if sequential_pids else pid_start - 1

    # The CSV only has HITS, not every PID that was checked, so re-grouping
    # them recovers each real cluster's actual span. Each group is then
    # routed by density (see CLUSTER_REPLAY_DENSITY_THRESHOLD's docstring):
    # dense groups become padded windows (cheap to fully re-sweep, catches
    # in-fill growth for free); sparse groups become an exact PID list
    # folded into targeted_pids instead (no point re-scanning mostly-empty
    # space every single run).
    cluster_windows = []
    sparse_cluster_pids = []
    for group in _group_pids_with_members(cluster_pids, CLUSTER_RECONSTRUCT_GAP_THRESHOLD):
        lo, hi = group[0], group[-1]
        density = len(group) / (hi - lo + 1)
        if density >= CLUSTER_REPLAY_DENSITY_THRESHOLD:
            cluster_windows.append((max(1, lo - CLUSTER_WINDOW_PAD), hi + CLUSTER_WINDOW_PAD))
        else:
            sparse_cluster_pids.extend(group)

    history = {
        "sequenced_range": {"low": pid_start, "high": sequenced_high},
        "cluster_windows": cluster_windows,
        "targeted_pids": sorted(set(targeted_pids) | set(sparse_cluster_pids)),
    }
    print(f"  reconstructed pid history from existing {csv_path}: "
          f"sequenced_range={history['sequenced_range']}, "
          f"{len(history['cluster_windows'])} cluster window(s) (dense), "
          f"{len(sparse_cluster_pids)} sparse-cluster pid(s) folded into direct replay, "
          f"{len(history['targeted_pids'])} targeted pid(s) total")
    return history


def scrape_town(town_slug: str, pid_start: int, pid_end: int, granit_geojson_path: str,
                 out_path: str, max_consecutive_misses: int = 300,
                 use_history: bool = True):
    """
    HISTORY REPLAY (added): before doing anything else, this checks
    whether out_path ALREADY EXISTS (i.e. this is a rerun for a town
    that's been scraped before) and, if so, reconstructs a 'known PID
    index' directly from that CSV's own match_source column (see
    _load_pid_history_from_csv) -- no separate history file to manage or
    forget to pass in. If found, Pass 1's blind range scan and Pass 2's
    per-address VGSI searches are skipped for every PID the old CSV
    already knew about; those get re-fetched directly by PID instead
    (_fetch_pid_list: one request each, no address-search variants).
    This is the fast path a MONTHLY re-run wants: assessed values change
    every year, but which PID an address lives at does not, so there's
    no need to re-discover it. Pass `use_history=False` to force a full
    fresh scan and ignore any existing out_path (e.g. after a known bad
    run, or the first time you suspect the town's PID layout shifted).

    Still runs every time, history or not:
      - a GROWTH PROBE just past the known sequenced range's high end
        (or, on a genuine first run, wherever Pass 1's own early-stop
        lands), to catch newly-added contiguous parcels (new
        construction in the town's main range) that would otherwise sit
        just past what the old CSV knew about.
      - the normal Pass 2a/2b discovery flow (cluster sweep, then
        per-address search), but only against whatever GRANIT addresses
        are STILL unmatched after replay + the growth probe -- i.e.
        genuinely new parcels not near anything already known. On a
        re-run this list should be short; on a first run (no existing
        CSV) it's everything Pass 1 didn't find, same as before.

    The output CSV this run writes becomes the history source for the
    NEXT run automatically -- nothing extra to save.

    Pass 1 (only runs when there's no prior CSV to replay from): walk
    PIDs sequentially across [pid_start, pid_end]. VGSI PIDs are dense
    but not perfectly contiguous (demolished/merged parcels leave gaps),
    so gaps are tolerated, but the scan bails out after a long
    consecutive run of misses -- a strong signal we've run past the top
    of the town's MAIN PID range (not necessarily the top of the town's
    real PID range -- see Pass 2 below).

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
    # ADDED 2026-08-30: fail fast if this town can't produce a single
    # parseable page at all -- see probe_layout()'s docstring. Cheap (~5
    # requests) insurance against repeating the Manchester scenario, where
    # a systemic issue wasn't discovered until 28,000 addresses deep into
    # Pass 2b.
    if not probe_layout(town_slug):
        raise RuntimeError(
            f"probe_layout failed for {town_slug!r}: none of the probe PIDs "
            f"produced a parseable page. Check the town_slug is correct, the "
            f"site is reachable, and (if both look fine) inspect a raw page "
            f"manually -- this may be a third layout variant not yet handled "
            f"by parse_parcel()."
        )

    all_rows: list[dict] = []
    # ADDED alongside _range_fully_covered: every contiguous PID range
    # actually scanned this run (sequenced replay/Pass 1, growth probe,
    # each cluster window) gets recorded here, so Pass 2a can skip
    # re-sweeping ground already covered by one of those instead of only
    # relying on find_missing_addresses' text matching to notice.
    scanned_ranges: list[tuple[int, int]] = []

    # MUST happen before we open anything in "w" mode, which truncates --
    # this is what lets the SAME out_path serve as both last run's output
    # and this run's history source.
    history = _load_pid_history_from_csv(out_path, pid_start) if use_history else None

    # ATOMIC WRITE (added): every row this run finds is written to a temp
    # file, NOT out_path directly. out_path itself is only touched once,
    # at the very end, via a single atomic os.replace() -- and only if
    # this function returns normally (no exception).
    #
    # Without this, out_path gets truncated to empty the moment it's
    # opened, and gets whatever partial set of rows had been written by
    # the time of a crash/kill/VGSIRequestStorm. That's a double problem:
    # (1) today's run is left with an incomplete CSV instead of
    # yesterday's complete one, and (2) the NEXT run reads that same
    # out_path as its history source (_load_pid_history_from_csv), so it
    # would "learn" that every PID missing from the partial file no
    # longer exists -- exactly backwards from the point of this feature,
    # which is to get FASTER on a retry, not to silently lose PIDs.
    #
    # A same-directory temp file + os.replace() is atomic on POSIX and on
    # Windows (both replace the destination in one filesystem operation,
    # not write-then-delete-then-rename) -- there's no window where
    # out_path is half-written. If this run fails partway through,
    # out_path is left exactly as the last SUCCESSFUL run left it, and
    # <out_path>.partial sticks around with whatever got scraped before
    # the failure, for post-mortem inspection -- it's overwritten (not
    # accumulated) by the next attempt, and never read as history.
    tmp_path = out_path + ".partial"

    with open(tmp_path, "w", newline="") as f:
        inner_writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        inner_writer.writeheader()
        writer = _DedupWriter(inner_writer)

        if history is not None:
            # ---- History replay: sequenced range, cluster windows, targeted PIDs ----
            seq = history["sequenced_range"]
            print("=" * 60)
            print(f"HISTORY REPLAY: re-fetching {seq['high'] - seq['low'] + 1} sequenced pid(s), "
                  f"{sum(hi - lo + 1 for lo, hi in history['cluster_windows'])} cluster pid(s), "
                  f"{len(history['targeted_pids'])} targeted pid(s) directly by pid...")
            print("=" * 60)

            replay_rows, replay_missing = _fetch_pid_list(
                town_slug, writer, range(seq["low"], seq["high"] + 1),
                match_source="sequential_replay", progress_label="replay sequenced",
            )
            all_rows.extend(replay_rows)
            sequenced_high = seq["high"]
            scanned_ranges.append((seq["low"], seq["high"]))
            print(f"  sequenced replay: {len(replay_rows)} hit, "
                  f"{len(replay_missing)} no longer resolve")

            for lo, hi in history["cluster_windows"]:
                rows, missing = _fetch_pid_list(
                    town_slug, writer, range(lo, hi + 1),
                    match_source="cluster_replay", progress_label=f"replay cluster {lo}-{hi}",
                )
                all_rows.extend(rows)
                scanned_ranges.append((lo, hi))
                print(f"  cluster {lo}-{hi} replay: {len(rows)} hit, "
                      f"{len(missing)} no longer resolve")

            if history["targeted_pids"]:
                rows, missing = _fetch_pid_list(
                    town_slug, writer, history["targeted_pids"],
                    match_source="targeted_replay", progress_label="replay targeted",
                )
                all_rows.extend(rows)
                print(f"  targeted replay: {len(rows)} hit, {len(missing)} no longer resolve "
                      f"(dropped -- no window to re-sweep for a single dead pid)")

            # FIXED: the growth probe used to always scan exactly
            # sequenced_high+1 .. sequenced_high+GROWTH_PROBE_WIDTH,
            # completely ignoring the pid_end the caller passed in. That
            # meant raising pid_end on the CLI (the obvious way to tell
            # this town's range needs to extend further) had NO effect
            # once history existed -- CONFIRMED via a real Manchester run
            # where pid_end was raised from 20000 to 50000 and the probe
            # still only checked up to sequenced_high+2000 regardless,
            # filling that entire fixed window with real hits (1891/2000,
            # no early-stop) -- a strong sign real data continued well
            # past it, with no way to reach it short of --fresh (which
            # would have thrown away ALL the good history just to widen
            # one number). pid_end now sets a FLOOR on how far the probe
            # is willing to look -- still bounded by
            # GROWTH_PROBE_MAX_MISSES's early-stop, so a town that HASN'T
            # grown doesn't pay for scanning all the way to pid_end, but
            # one that has can now actually be told to look further.
            growth_probe_hi = max(sequenced_high + GROWTH_PROBE_WIDTH, pid_end)
            print("=" * 60)
            print(f"GROWTH PROBE: checking pid {sequenced_high + 1}-"
                  f"{growth_probe_hi} for newly-added parcels...")
            print("=" * 60)
            growth_rows, growth_last_pid = _scan_pid_range(
                town_slug, writer, sequenced_high + 1, growth_probe_hi,
                match_source="sequential", max_consecutive_misses=GROWTH_PROBE_MAX_MISSES,
                progress_label=f"growth probe {sequenced_high + 1}-{growth_probe_hi}",
            )
            all_rows.extend(growth_rows)
            scanned_ranges.append((sequenced_high + 1, growth_last_pid))
            if growth_rows:
                print(f"  growth probe: {len(growth_rows)} new parcel(s) found up to pid "
                      f"{max(int(r['pid']) for r in growth_rows if str(r['pid']).isdigit())} "
                      f"-- next run's replay will cover them directly")
            else:
                print("  growth probe: no new parcels found")
            print("=" * 60)
            print(f"HISTORY REPLAY done: {len(all_rows)} parcels re-confirmed/found without "
                  f"per-address search.")
            print("=" * 60)
        else:
            # ---- Pass 1 (no prior CSV to replay from -- first run for this town, or
            # use_history=False forced a fresh scan) ----
            pass1_rows, pass1_last_pid = _scan_pid_range(
                town_slug, writer, pid_start, pid_end,
                match_source="sequential", max_consecutive_misses=max_consecutive_misses,
                progress_label=f"Pass 1, pid {pid_start}-{pid_end}",
            )
            all_rows.extend(pass1_rows)
            scanned_ranges.append((pid_start, pass1_last_pid))

            print("=" * 60)
            print(f"Pass 1 done: {len(pass1_rows)} parcels staged to {tmp_path}")
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

        print(f"PASS 2a: {len(missing_addresses)} GRANIT addresses have no match so far -- "
              f"sampling up to {CLUSTER_DISCOVERY_SAMPLE_SIZE} to discover any out-of-range "
              f"PID clusters...")
        print("=" * 60)

        if missing_addresses:
            discovery_sample = missing_addresses[:CLUSTER_DISCOVERY_SAMPLE_SIZE]
            discovered_pids = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(lookup_and_fetch, town_slug, address): address
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
                    if _range_fully_covered(window_lo, window_hi, scanned_ranges):
                        print(f"  cluster window pid {window_lo}-{window_hi} already fully "
                              f"scanned this run (sequenced/growth-probe/cluster replay covers "
                              f"it) -- skipping redundant sweep. Any address here still showing "
                              f"'missing' is almost certainly a GRANIT address-text mismatch "
                              f"(e.g. a road-only entry with no house number), not an un-scanned "
                              f"PID.")
                        continue
                    print(f"  sweeping cluster window pid {window_lo}-{window_hi}...")
                    swept, _swept_last_pid = _scan_pid_range(
                        town_slug, writer, window_lo, window_hi,
                        match_source="cluster_sweep", max_consecutive_misses=None,
                        progress_label=f"cluster {window_lo}-{window_hi}",
                    )
                    all_rows.extend(swept)
                    scanned_ranges.append((window_lo, window_hi))
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

        # SPEEDUP: MAX_WORKERS pool, each worker using its own thread-local
        # session (see _get_thread_session), instead of one address at a
        # time with a blocking time.sleep(0.3). Order of the printed
        # [i/n] lines is no longer guaranteed to match still_missing's
        # original order (results print as they complete), but every
        # address is still looked up exactly once and every row still gets
        # written -- only the console ordering changed, not the output.
        if still_missing:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(lookup_and_fetch, town_slug, address): address
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
                        if writer.writerow(row):
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

    # Only reached if every pass above completed without raising (e.g. no
    # VGSIRequestStorm). This is the ONE moment out_path itself is
    # touched -- os.replace() is atomic, so out_path either still holds
    # the previous complete run's data (if we never get here) or
    # this run's complete data (once this line finishes) -- never
    # something in between.
    os.replace(tmp_path, out_path)

    print(f"FINAL: {len(all_rows)} total parcels written to {out_path}.")
    print(f"  ({out_path} is also this run's history source for the NEXT run -- "
          f"nothing extra to save.)")
    print("=" * 60)

    return all_rows


def main():
    # ADDED: optional --fresh flag. Without it, if <output.csv> already
    # exists from a prior run of this same town, this run automatically
    # reads it first (see _load_pid_history_from_csv) and replays its
    # known PIDs directly instead of repeating Pass 1's blind scan and
    # Pass 2's per-address searches -- same command, same output path,
    # just faster the second time onward. Pass --fresh to ignore any
    # existing CSV and force a full scan from scratch (e.g. after a run
    # you don't trust, or if you suspect the town's PID layout shifted).
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    use_history = "--fresh" not in sys.argv

    if len(args) != 5:
        print("Usage: python vgsi_assessment_scraper.py <town_slug> <pid_start> <pid_end> "
              "<granit_parcels.geojson> <output.csv> [--fresh]")
        print("Example (first run, or any monthly re-run -- same command either way):")
        print("  python vgsi_assessment_scraper.py lincolnnh 1 3000 lincoln_nh.geojson "
              "lincoln_assessments.csv")
        print("Example (force a full fresh scan, ignoring the existing output.csv):")
        print("  python vgsi_assessment_scraper.py lincolnnh 1 3000 lincoln_nh.geojson "
              "lincoln_assessments.csv --fresh")
        sys.exit(1)

    town_slug = args[0]
    pid_start, pid_end = int(args[1]), int(args[2])
    granit_geojson_path = args[3]
    out_path = args[4]

    scrape_town(town_slug, pid_start, pid_end, granit_geojson_path, out_path,
                use_history=use_history)


if __name__ == "__main__":
    main()