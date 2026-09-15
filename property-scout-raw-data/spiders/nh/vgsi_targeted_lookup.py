"""
VGSI targeted address lookup — ValueGap (NH gap-fill path)

Looks up ONE address at a time against VGSI's own address-autocomplete
search and returns that parcel's assessed value. This is NOT a town
sweep -- it costs one search + one page fetch per address you actually
call it for, unlike the old vgsi_assessment_scraper.py's Pass 1/2a/2b
full-town scan (removed 2026-09, along with join_parcels_assessments.py
-- NH's main value pipeline is now offmarket-only, see nh_spider.py).

INTENDED USE: an optional, manually-run gap-fill for parcels that came
back with no value from the offmarket point-in-polygon join
(join_parcels_offmarket.py) -- NOT wired into nh_spider.py's automatic
flow. Call has_vgsi_coverage(town) FIRST for any town you're about to
gap-fill -- VGSI is not NH's statewide assessor platform, only some
towns use it (CONFIRMED list below, 2026-09). Calling lookup_and_fetch()
against an uncovered town (e.g. Freedom) doesn't fail fast -- the search
request just times out, indistinguishable at a glance from a slow
network, and burns 15s per address for a town that will NEVER resolve.
Check coverage once per town, not per address.

MERGED 2026-09: parse_parcel() and normalize_address() used to live in
vgsi_assessment_scraper.py and were imported from there. That file's
only other job was the full-town sweep this script doesn't do, so
rather than keep two files alive for two functions, they're defined
directly below -- one VGSI file instead of two to maintain.

Usage:
    from vgsi_targeted_lookup import lookup_and_fetch, has_vgsi_coverage
    if has_vgsi_coverage("Freedom"):
        parsed, status = lookup_and_fetch("freedomnh", "144 West Bay Road")
    else:
        print("Freedom has no VGSI coverage -- skip, nothing to gap-fill here")
"""

import json
import re
import threading

import requests
from bs4 import BeautifulSoup

from pathlib import Path
import sys

# Shared property-type standardization, used by every state spider (see
# spiders/common/property_types.py's module docstring). Added to sys.path
# the same way nh_spider.py adds its own directory for granit_parcel_downloader.py
# etc. -- keeps this script runnable/importable without assuming a package context.
_COMMON_DIR = Path(__file__).parent.parent / "common"
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR))
from property_types import standardize_property_type

SEARCH_URL = "https://gis.vgsi.com/{town}/async.asmx/GetDataAddress"
PARCEL_URL = "https://gis.vgsi.com/{town}/Parcel.aspx?Pid={pid}"
HEADERS = {
    "User-Agent": "ValueGap research tool (personal project, low volume)",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

# CONFIRMED (2026-09, Thale): VGSI is NOT NH's statewide assessor platform
# -- only these towns actually use it. A town not on this list (e.g.
# Freedom) has no VGSI site at all; calling lookup_and_fetch() against it
# doesn't fail cleanly, the search request just times out (15s per address
# attempted, for a town that will never resolve). Check has_vgsi_coverage()
# once per town before gap-filling any of its unmatched parcels.
VGSI_COVERED_TOWNS = {
    "Amherst", "Bedford", "Berlin", "Bow", "Bridgewater", "Charlestown", "Claremont",
    "Concord", "Derry", "Durham", "Epping", "Exeter", "Fremont", "Goffstown",
    "Grantham", "Greenland", "Hampton", "Hollis", "Hooksett", "Hudson", "Jaffrey",
    "Keene", "Lebanon", "Laconia", "Lincoln", "Londonderry", "Lyme", "Manchester",
    "Meredith", "Milford", "Newington", "Newmarket", "North Hampton", "Pelham",
    "Peterborough", "Portsmouth", "Raymond", "Rye", "Salem", "Seabrook", "Strafford",
}
_VGSI_COVERED_TOWNS_LOWER = {t.lower() for t in VGSI_COVERED_TOWNS}


def has_vgsi_coverage(town: str) -> bool:
    """True if `town` (case-insensitive, e.g. 'freedom' or 'Freedom') is a
    known VGSI-covered NH town. Call this before lookup_and_fetch() for
    any town -- see module docstring for why an uncovered town times out
    instead of failing fast."""
    return town.strip().lower() in _VGSI_COVERED_TOWNS_LOWER


def guess_town_slug(town: str) -> str:
    """VGSI's URL slug convention: lowercase, no spaces, +'nh' (e.g.
    'North Hampton' -> 'northhamptonnh'). Moved here from nh_spider.py's
    old _guess_vgsi_town_slug when VGSI came out of the main pipeline --
    anything calling lookup_and_fetch()/search_address() needs a slug,
    not a town name, so this belongs alongside them."""
    return town.lower().replace(" ", "") + "nh"

_thread_local = threading.local()


def _get_thread_session() -> requests.Session:
    """One requests.Session per worker thread, not a shared one -- a
    single shared Session()'s connection pool is not safe under
    concurrent use against VGSI's stateful backend (confirmed root cause
    of an earlier ASP.NET session-lock contention / HTTP 500 storm issue
    in this project)."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


# ---------------------------------------------------------------------------
# Parcel page parsing (moved from vgsi_assessment_scraper.py, 2026-09 merge)
# ---------------------------------------------------------------------------

def parse_parcel(html: str) -> dict | None:
    """Pull the fields we need out of a VGSI parcel page's visible text.

    Handles both VGSI page layouts: old ("Total Market Value" label,
    "PID" present as visible text) and new ("Assessment" label, "PID"
    NOT present as visible text on some towns). Gate accepts either
    layout -- "Location" is present on both, "Total Market Value" (old)
    or "Assessment" (new) confirms it's a real populated record and not
    a blank/error page.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

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
    # "Residential Land") -- the assessor's own plain-English classification.
    land_use_section = text.split("Land Use", 1)
    land_use_desc = None
    if len(land_use_section) > 1:
        desc_m = re.search(r"Description\s*\n+\s*(.+?)\s*\n", land_use_section[1])
        if desc_m:
            land_use_desc = standardize_property_type(desc_m.group(1).strip())

    return {
        "location": location_m.group(1).strip() if location_m else None,
        # Try the OLD layout's label first ("Total Market Value"), falling
        # back to the NEW layout's first "Assessment" occurrence if the old
        # label isn't present. Checking the more specific string first
        # avoids matching something unintended on a new-layout page that
        # happens to also contain "Assessment" elsewhere before the real value.
        "total_market_value": grab("Total Market Value") or grab("Assessment"),
        # Legitimately comes back None on old-layout pages, since that label
        # isn't rendered as visible text there -- expected, not a bug.
        # _fetch_and_parse below overwrites parsed["pid"] = pid from the
        # search result regardless, so nothing downstream depends on this.
        "pid": grab("PID", r"(\d+)"),
        "mblu": grab("Mblu", r"([\d/ ]+)"),
        "land_use_desc": land_use_desc,
        "acres": grab("Size (Acres)", r"([\d.]+)"),
        "raw_text_ok": True,
    }


DIRECTIONAL_EXPAND = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST"}
DIRECTIONAL_ABBREV = {v: k for k, v in DIRECTIONAL_EXPAND.items()}
SUFFIX_EXPAND = {"RD": "ROAD", "ST": "STREET", "LN": "LANE", "DR": "DRIVE", "AVE": "AVENUE",
                  "MTN": "MOUNTAIN", "TRL": "TRAIL", "CIR": "CIRCLE", "CT": "COURT",
                  "BLVD": "BOULEVARD", "HWY": "HIGHWAY", "PL": "PLACE"}
SUFFIX_ABBREV = {v: k for k, v in SUFFIX_EXPAND.items()}


def normalize_address(raw: str) -> str:
    """Uppercase, strip punctuation, collapse whitespace, abbreviate
    suffixes -- so 'South Peak Road' and 'S PEAK RD' compare equal.
    Uses SUFFIX_ABBREV (full word -> abbreviation, e.g. ROAD -> RD) --
    NOT SUFFIX_EXPAND -- to match vgsi_assessment_scraper.py's original
    _SUFFIX_MAP direction exactly."""
    if not raw:
        return ""
    s = re.sub(r"[^\w\s]", " ", raw.upper())
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [SUFFIX_ABBREV.get(tok, tok) for tok in s.split(" ")]
    return " ".join(tokens)


def address_variants(address):
    # Strip punctuation before tokenizing (fixes: "St. Mary's Lane" used to
    # only generate 2/4 expected variants, since "ST." != "ST" as a dict key).
    tokens = re.sub(r"[^\w\s]", " ", address.upper()).split()
    def apply(direction_map, suffix_map):
        return " ".join(suffix_map.get(direction_map.get(t, t), direction_map.get(t, t)) for t in tokens)
    identity = {}
    variants = []
    for dmap in (identity, DIRECTIONAL_EXPAND, DIRECTIONAL_ABBREV):
        for smap in (identity, SUFFIX_EXPAND, SUFFIX_ABBREV):
            v = apply(dmap, smap)
            if v not in variants:
                variants.append(v)
    normalized_original = apply(identity, identity)
    if normalized_original in variants:
        variants.remove(normalized_original)
    return [address] + variants


def search_address(town_slug, address):
    session = _get_thread_session()
    resp = session.post(SEARCH_URL.format(town=town_slug), headers=HEADERS,
                         data=json.dumps({"inVal": address, "src": "i_address"}), timeout=15)
    resp.raise_for_status()
    return resp.json().get("d", [])


def _fetch_and_parse(town_slug, pid):
    """Returns (parsed_or_None, error_string_or_None)."""
    session = _get_thread_session()
    try:
        resp = session.get(PARCEL_URL.format(town=town_slug, pid=pid),
                            headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
        resp.raise_for_status()
        parsed = parse_parcel(resp.text)
    except requests.RequestException as e:
        return None, f"fetch_failed: {e}"
    if parsed is None:
        return None, "fetch_failed: page didn't parse as a valid parcel"
    parsed["pid"] = pid
    return parsed, None


def lookup_and_fetch(town_slug, address):
    """
    Search variants until one resolves cleanly, PREFERRING a variant whose
    candidates include an exact normalized-text match over one that only
    returns a prefix/partial match.

    Per variant, in order:
      1. Any candidate whose own address text normalizes to exactly the
         query variant -> use it (status 'ok'), even if other, non-matching
         candidates were also returned.
      2. Else exactly one candidate returned -> use it (status 'ok').
      3. Else (multiple, none exact) -> remember as a fallback, keep trying
         other variants.
    If no variant ever satisfies 1 or 2, fall back to the first candidate
    of the first non-empty variant seen (status 'ambiguous'), with the
    status including the actual candidate address list so an ambiguous
    match can be spot-checked without re-running anything by hand.
    """
    fallback_matches = None
    fallback_variant = None

    for variant in address_variants(address):
        try:
            matches = search_address(town_slug, variant)
        except requests.RequestException as e:
            return None, f"search_failed: {e}"

        if not matches:
            continue

        exact = [m for m in matches if normalize_address(m.get("value", "")) == normalize_address(variant)]
        chosen = None
        if exact:
            chosen = exact[0]
        elif len(matches) == 1:
            chosen = matches[0]

        if chosen is not None:
            status = "ok"
            if variant != address:
                status += f' (via "{variant}")'
            parsed, err = _fetch_and_parse(town_slug, chosen["id"])
            if err:
                return None, err
            return parsed, status

        if fallback_matches is None:
            fallback_matches = matches
            fallback_variant = variant

    if fallback_matches is None:
        return None, "no_match"

    status = "ambiguous"
    if fallback_variant != address:
        status += f' (via "{fallback_variant}")'
    candidate_summary = "; ".join(m.get("value", "?") for m in fallback_matches[:5])
    status += f" -- candidates: [{candidate_summary}]"
    parsed, err = _fetch_and_parse(town_slug, fallback_matches[0]["id"])
    if err:
        return None, err
    return parsed, status