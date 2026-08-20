import sys, csv, time, json, re
import requests
from vgsi_assessment_scraper import parse_parcel, normalize_address

SEARCH_URL = "https://gis.vgsi.com/{town}/async.asmx/GetDataAddress"
PARCEL_URL = "https://gis.vgsi.com/{town}/Parcel.aspx?Pid={pid}"
HEADERS = {
    "User-Agent": "ValueGap research tool (personal project, low volume)",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

DIRECTIONAL_EXPAND = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST"}
DIRECTIONAL_ABBREV = {v: k for k, v in DIRECTIONAL_EXPAND.items()}
SUFFIX_EXPAND = {"RD": "ROAD", "ST": "STREET", "LN": "LANE", "DR": "DRIVE", "AVE": "AVENUE",
                  "MTN": "MOUNTAIN", "TRL": "TRAIL", "CIR": "CIRCLE", "CT": "COURT",
                  "BLVD": "BOULEVARD", "HWY": "HIGHWAY", "PL": "PLACE"}
SUFFIX_ABBREV = {v: k for k, v in SUFFIX_EXPAND.items()}


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
    resp = requests.post(SEARCH_URL.format(town=town_slug), headers=HEADERS,
                          data=json.dumps({"inVal": address, "src": "i_address"}), timeout=15)
    resp.raise_for_status()
    return resp.json().get("d", [])


def _fetch_and_parse(town_slug, pid):
    """Returns (parsed_or_None, error_string_or_None)."""
    try:
        resp = requests.get(PARCEL_URL.format(town=town_slug, pid=pid),
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

    Previously: any non-empty result set stopped the search immediately,
    even a multi-candidate prefix match (e.g. "6 POLLARD ROAD" also
    matching "6 POLLARD ROAD EXT") -- a later, more specific variant might
    have come back with exactly one clean hit but was never tried, because
    the search stopped too early. This version keeps trying variants when
    a match is ambiguous, and only falls back to "take the first" if NO
    variant ever resolves unambiguously.

    Per variant, in order:
      1. Any candidate whose own address text normalizes to exactly the
         query variant -> use it (status 'ok'), even if other, non-matching
         candidates were also returned.
      2. Else exactly one candidate returned -> use it (status 'ok').
      3. Else (multiple, none exact) -> remember as a fallback, keep trying
         other variants.
    If no variant ever satisfies 1 or 2, fall back to the first candidate
    of the first non-empty variant seen (status 'ambiguous'), same
    end-result as before, but now the status includes the actual candidate
    address list so an ambiguous match can be spot-checked without needing
    to re-run anything by hand.
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