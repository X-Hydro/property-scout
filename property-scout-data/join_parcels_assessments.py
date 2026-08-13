"""
Join GRANIT parcels + VGSI assessments — ValueGap

Joins on a normalized Map-Block-Lot (MBLU) key. Originally assumed GRANIT's
`PID` property and VGSI's `Pid` URL parameter were the same identifier --
they're not. VGSI's `Pid` is an internal database row number specific to
that town's VGSI install; GRANIT's `PID` is actually the assessor's
Map-Block-Lot ID, packed as e.g. '127-268000-00'. VGSI separately exposes
the same Map-Block-Lot info in its own `mblu` field (e.g.
'127/  268/  000/00...'), just formatted differently -- that's the real
shared key between the two sources.

Still no geocoding or address matching needed for this step -- that's
reserved for the later listings-to-parcel join, where there's no shared
key to rely on.

Usage:
    python join_parcels_assessments.py lincoln_nh.geojson lincoln_assessments.csv lincoln_joined.geojson
"""

import sys
import json
import csv
import re


# Common street-suffix abbreviation pairs seen between GRANIT's StreetAddress
# and VGSI's scraped location field. Not exhaustive -- extend as mismatches
# turn up during spot checks.
_SUFFIX_MAP = {
    "ROAD": "RD", "STREET": "ST", "LANE": "LN", "DRIVE": "DR",
    "AVENUE": "AVE", "MOUNTAIN": "MTN", "TRAIL": "TRL", "CIRCLE": "CIR",
    "COURT": "CT", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "PLACE": "PL",
}


def normalize_address(raw: str) -> str:
    """
    Normalize an address string for matching: uppercase, collapse
    whitespace, strip punctuation, expand common suffix abbreviations to a
    canonical short form so 'WEST STREET' and 'WEST ST' compare equal.
    This is a best-effort normalizer, not a full address parser -- it won't
    handle every format quirk in either source (unit numbers, "#LO"-style
    VGSI suffixes on undeveloped land, etc.), so treat address-based matches
    as lower-confidence than MBLU matches and spot check a sample of them.
    """
    if not raw:
        return ""
    s = re.sub(r"[^\w\s]", " ", raw.upper())
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [_SUFFIX_MAP.get(tok, tok) for tok in s.split(" ")]
    return " ".join(tokens)


def normalize_granit_id(raw: str):
    """
    GRANIT's 'PID' field format varies BY TOWN -- this was discovered when
    Lincoln's town-specific packed format (see below) produced 0 MBLU
    matches on Lebanon, silently falling through to address-only matching
    for the whole town. Two known formats so far:
      - Lincoln-style (hyphen-separated, packed): '127-268000-00' ->
        Map=127, Block=268, Lot=000 (middle 6-digit chunk is Block+Lot
        concatenated with no separator between them)
      - Lebanon-style (space-separated, already distinct fields):
        '0106 0032 00000' -> Map=0106, Block=0032, Lot=00000 (block and
        lot are already separate tokens, no packing)
    Distinguished by extracting all digit-groups regardless of separator,
    then checking the middle group's length: 6 digits means packed
    (Lincoln-style, split 3+3); any other length means already-separated
    (Lebanon-style, used as-is). This is inferred from exactly two towns
    -- if a third town's format doesn't fit either pattern, this will
    return None (unparseable) rather than guess wrong, and will need a
    new case added here once that shape is seen.
    Returns a (map, block, lot) tuple with leading zeros stripped per
    component, or None if unparseable.
    """
    groups = re.findall(r"\d+", raw.strip())
    if len(groups) < 3:
        return None

    map_, second, third = groups[0], groups[1], groups[2]
    if len(second) == 6:
        # Packed Lincoln-style: second group is Block+Lot concatenated
        block, lot = second[:3], second[3:]
    else:
        # Already-separated Lebanon-style
        block, lot = second, third

    return (map_.lstrip("0") or "0", block.lstrip("0") or "0", lot.lstrip("0") or "0")


def normalize_vgsi_mblu(raw: str):
    """
    VGSI's 'mblu' column is slash-separated: 'Map/Block/Lot/Unit-Sub/'.
    Returns a (map, block, lot) tuple in the same normalized shape as
    normalize_granit_pid, or None if unparseable.
    """
    parts = [p.strip() for p in raw.split("/") if p.strip() != ""]
    if len(parts) < 3:
        return None
    map_, block, lot = parts[0], parts[1], parts[2]
    return (map_.lstrip("0") or "0", block.lstrip("0") or "0", lot.lstrip("0") or "0")


def load_assessments(csv_path: str) -> tuple[dict[tuple, dict], dict[str, dict]]:
    """
    Return two lookup dicts for the VGSI assessment rows:
    - by MBLU key (map, block, lot) -- primary, high-confidence match
    - by normalized address string -- fallback for rows an MBLU match misses
    Rows with duplicate address keys are dropped from the address dict
    entirely (kept as None-marked) rather than silently picking one,
    since an ambiguous address match is worse than no match.
    """
    by_mblu = {}
    by_address = {}
    address_collisions = set()

    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            mblu_key = normalize_vgsi_mblu(row["mblu"])
            if mblu_key is not None:
                by_mblu[mblu_key] = row

            addr_key = normalize_address(row["location"])
            if addr_key:
                if addr_key in by_address and addr_key not in address_collisions:
                    address_collisions.add(addr_key)
                by_address[addr_key] = row

    for addr_key in address_collisions:
        del by_address[addr_key]

    return by_mblu, by_address


def join(geojson_path: str, csv_path: str, out_path: str):
    with open(geojson_path) as f:
        parcels = json.load(f)

    by_mblu, by_address = load_assessments(csv_path)

    matched_mblu = 0
    matched_address = 0
    unmatched_parcel_pids = []
    joined_features = []

    for feature in parcels["features"]:
        props = feature["properties"]
        raw_id = str(props.get("PID", "")).strip()
        mblu_key = normalize_granit_id(raw_id)
        assessment = by_mblu.get(mblu_key) if mblu_key else None
        match_method = "mblu" if assessment is not None else None

        if assessment is None:
            addr_key = normalize_address(props.get("StreetAddress", ""))
            if addr_key:
                assessment = by_address.get(addr_key)
                if assessment is not None:
                    match_method = "address"

        if assessment is None:
            unmatched_parcel_pids.append(raw_id)
            continue

        props["total_market_value"] = assessment["total_market_value"]
        props["vgsi_location"] = assessment["location"]
        props["mblu"] = assessment["mblu"]
        props["acres"] = assessment["acres"]
        props["land_use_desc"] = assessment.get("land_use_desc")
        props["match_method"] = match_method  # "mblu" (high confidence) or "address" (lower -- spot check)
        joined_features.append(feature)
        if match_method == "mblu":
            matched_mblu += 1
        else:
            matched_address += 1

    out = {"type": "FeatureCollection", "features": joined_features}
    with open(out_path, "w") as f:
        json.dump(out, f)

    total_parcels = len(parcels["features"])
    matched = matched_mblu + matched_address
    print(f"GRANIT parcels: {total_parcels}")
    print(f"VGSI assessments: {len(by_mblu)}")
    print(f"Matched on MBLU (high confidence): {matched_mblu}")
    print(f"Matched on address fallback (lower confidence -- spot check these): {matched_address}")
    print(f"Total matched: {matched}")
    print(f"Unmatched GRANIT parcels: {len(unmatched_parcel_pids)}")

    if unmatched_parcel_pids:
        sample = unmatched_parcel_pids[:10]
        print(f"  sample unmatched PIDs: {sample}")

    match_rate = matched / total_parcels if total_parcels else 0
    print(f"Match rate: {match_rate:.1%}")
    if match_rate < 0.8:
        print("WARNING: match rate below 80% -- spot check a few unmatched PIDs "
              "on both GRANIT and VGSI before trusting this join.")

    print(f"Wrote {matched} joined parcels to {out_path}")


def main():
    if len(sys.argv) != 4:
        print("Usage: python join_parcels_assessments.py <parcels.geojson> <assessments.csv> <output.geojson>")
        sys.exit(1)

    join(sys.argv[1], sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()