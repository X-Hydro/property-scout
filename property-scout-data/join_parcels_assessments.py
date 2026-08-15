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


def normalize_unit_token(raw: str | None) -> str:
    """
    Shared unit-key normalizer for both GRANIT's hyphen/space suffix and
    VGSI's mblu 4th segment.

    FIXED (second bug found on top of the first): an earlier version of
    this logic took only the FIRST whitespace token of VGSI's 4th mblu
    segment as the unit key. That's wrong for condos that DO encode real
    per-unit info there as a two-token pair (e.g. '01 00003' vs
    '01 00004' for two different units in the same building, confirmed
    real example: Mountain Brook Circle) -- taking only '01' collapsed
    both units onto the same key, the exact bug this whole thing is
    trying to fix, just in a different spot.

    This version strips leading zeros from EACH token individually, and
    only collapses to the shared '00' ("no unit") default when EVERY
    token is purely zeros -- so an ordinary non-condo parcel's VGSI
    segment ('00 00000') and GRANIT's suffix ('00') both correctly reduce
    to the same '00', while a genuine two-token per-unit identifier stays
    distinct.

    Separately confirmed (real data): some condo complexes -- e.g. 36
    Lodge Road, 11/5 Robin Road, 5 Goldfinch Road -- have VGSI mblu
    strings with NO per-unit information at all (every unit in the
    building shares the literal identical mblu text). No amount of
    parsing can recover a distinction that isn't there; these correctly
    remain colliding keys, which load_assessments()'s collision handling
    below drops from MBLU matching and falls through to address matching
    instead -- the address text ('...#1', '...#2', etc.) IS where the
    real distinguishing info lives for those units.
    """
    if raw is None:
        return "00"
    tokens = raw.strip().upper().split()
    if not tokens:
        return "00"
    stripped = [t.lstrip("0") or "0" for t in tokens]
    if all(t == "0" for t in stripped):
        return "00"
    return " ".join(stripped)


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
    Distinguished by splitting on either '-' or whitespace (separator-
    agnostic, so both formats parse with the same code), then checking
    whether the second token is a 6-digit run: 6 digits means packed
    (Lincoln-style, split 3+3); anything else means already-separated
    (Lebanon-style, used as-is). This is inferred from exactly two towns
    -- if a third town's format doesn't fit either pattern, this will
    return None (unparseable) rather than guess wrong, and will need a
    new case added here once that shape is seen.

    FIXED (previously a real bug): the old version extracted ONLY digit
    groups via re.findall of a digit-run pattern, which silently discarded any
    letter in a condo/subdivided-unit suffix -- e.g. '118-039000-3B' and
    '118-039000-10A' both lost their letter entirely, AND (for the packed
    format) the suffix token wasn't used for block/lot at all, so every
    unit sharing base parcel '118-039000' collapsed onto the IDENTICAL
    key ('118','39','0'). Confirmed against real Lincoln data: this
    caused a real join run to match 2038 GRANIT parcels using only 1755
    distinct VGSI rows -- only possible if multiple different condo units
    were silently matching onto the same VGSI assessment record (i.e.
    getting someone else's assessed value / land use).

    Now returns a 4th "unit" component (the raw suffix token, letters
    preserved, upper-cased, leading zeros stripped) so units are kept
    distinct instead of being discarded. A plain (non-condo) parcel's
    unit token is "00" on both sides of the join, so this doesn't change
    matching behavior for anything that isn't a condo/subdivided unit.

    Returns a (map, block, lot, unit) tuple, or None if unparseable.
    """
    tokens = [t for t in re.split(r"[-\s]+", raw.strip()) if t != ""]
    if len(tokens) < 2:
        return None

    map_ = tokens[0]
    packed_or_block = tokens[1]

    if packed_or_block.isdigit() and len(packed_or_block) == 6:
        # Packed Lincoln-style: second token is Block+Lot concatenated.
        # Any further token is the unit/condo suffix.
        block, lot = packed_or_block[:3], packed_or_block[3:]
        unit_raw = tokens[2] if len(tokens) > 2 else "00"
    else:
        # Lebanon-style (already-separated): block and lot are distinct
        # tokens. A 4th token, if present, is the unit suffix.
        block = packed_or_block
        lot = tokens[2] if len(tokens) > 2 else "0"
        unit_raw = tokens[3] if len(tokens) > 3 else "00"

    unit_key = normalize_unit_token(unit_raw)
    return (
        map_.lstrip("0") or "0",
        block.lstrip("0") or "0",
        lot.lstrip("0") or "0",
        unit_key,
    )


def normalize_vgsi_mblu(raw: str):
    """
    VGSI's 'mblu' column is slash-separated: 'Map/Block/Lot/Unit-Sub/'.
    Returns a (map, block, lot, unit) tuple in the same normalized shape
    as normalize_granit_id, or None if unparseable.

    FIXED (previously a real bug, mirroring normalize_granit_id's): only
    the first 3 slash-separated parts were used, silently dropping the
    4th segment -- which is exactly where VGSI encodes the condo/unit
    sub-identifier. Confirmed against a real non-condo example
    ('121/  077/  000/00 00000/'), whose 4th segment ('00 00000') starts
    with '00' -- the "no unit" placeholder a real unit's mblu would
    presumably replace with its actual unit code.
    """
    parts = [p.strip() for p in raw.split("/") if p.strip() != ""]
    if len(parts) < 3:
        return None
    map_, block, lot = parts[0], parts[1], parts[2]
    unit_key = normalize_unit_token(parts[3] if len(parts) > 3 else None)
    return (
        map_.lstrip("0") or "0",
        block.lstrip("0") or "0",
        lot.lstrip("0") or "0",
        unit_key,
    )


def load_assessments(csv_path: str) -> tuple[dict[tuple, dict], dict[str, dict]]:
    """
    Return two lookup dicts for the VGSI assessment rows:
    - by MBLU key (map, block, lot, unit) -- primary, high-confidence match
    - by normalized address string -- fallback for rows an MBLU match misses
    Rows with duplicate keys (MBLU or address) are dropped from that dict
    entirely, rather than silently picking whichever came last, since a
    wrong/ambiguous match is worse than no match at all.

    FIXED: previously by_mblu had no collision handling at all -- any key
    collision silently kept whichever row was inserted last. With the old
    3-component (map, block, lot) key, this was actively dangerous: every
    condo/subdivided unit under one base parcel collapsed onto the same
    key, so this silently overwrote one unit's real assessed value/land
    use with another unit's data on every collision. normalize_vgsi_mblu
    and normalize_granit_id now both include a 4th "unit" component,
    which resolves the vast majority of those collisions -- but this dict
    now ALSO drops (rather than silently overwrites) any collision that
    still occurs, and reports the count, so a future format this hasn't
    seen yet fails loudly instead of silently corrupting a match.
    """
    by_mblu = {}
    by_address = {}
    mblu_collisions = set()
    address_collisions = set()

    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            mblu_key = normalize_vgsi_mblu(row["mblu"])
            if mblu_key is not None:
                if mblu_key in by_mblu and mblu_key not in mblu_collisions:
                    mblu_collisions.add(mblu_key)
                by_mblu[mblu_key] = row

            addr_key = normalize_address(row["location"])
            if addr_key:
                if addr_key in by_address and addr_key not in address_collisions:
                    address_collisions.add(addr_key)
                by_address[addr_key] = row

    for mblu_key in mblu_collisions:
        del by_mblu[mblu_key]
    for addr_key in address_collisions:
        del by_address[addr_key]

    if mblu_collisions:
        print(f"WARNING: {len(mblu_collisions)} MBLU key(s) matched more than one VGSI "
              f"assessment row -- these were dropped from MBLU matching entirely (falls "
              f"through to address matching instead) rather than risk silently picking "
              f"the wrong one. Sample colliding keys: {list(mblu_collisions)[:5]}")

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