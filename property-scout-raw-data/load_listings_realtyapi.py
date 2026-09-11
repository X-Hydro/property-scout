"""
RealtyAPI (realtyapi.io -> Realtor.com) listings loader — Property Values Database

Second-provider loader alongside load_listings.py (RentCast). Loads into the
SAME `listings` table, via the SAME upsert_listings() function -- this file
does NOT duplicate the upsert SQL, same "one write path" convention as the
RentCast loader.

FIELD MAPPING IS CONFIRMED against a real /search/byzip response (Stoneham,
MA 02180, single_family + land, resultCount=200 -> 40 results, all
single_family, all for_sale, one page). Compared side-by-side against
RentCast's nh_lincoln_sfh_land.json sample. Key differences from RentCast,
baked into the mapping below:

  - RealtyAPI's search response is flat (top-level beds/baths/sqft/lot_sqft/
    list_price/list_date/status/property_type), with only `address` nested
    (line/city/state_code/postal_code/latitude/longitude) and `county` as a
    flat string -- NOT nested under description/location blocks like some
    Realtor.com internal APIs do.
  - `baths` comes back as a STRING ("2"), not a number -- cast defensively.
  - property_type / status use different vocab than RentCast
    ("single_family"/"for_sale" vs RentCast's "Single Family"/"Active").
    PROPERTY_TYPE_MAP / STATUS_MAP below normalize RealtyAPI's values to
    RentCast's spelling so the shared `listings` table has one consistent
    vocabulary regardless of source.
  - The /search/byzip response does NOT include: year_built, days_on_market,
    mls_name, mls_number, or price_history. These appear to be
    /details/byid-only fields (which cost separate credits per listing --
    1 credit/call). They load as NULL here.
  - `advertisers` is a list of dicts, each with `office` as a plain string
    (not RentCast's separate nested listingOffice object with phone/email).
    Stored whole in the `agent` column; `office` column is left NULL rather
    than force a mismatched shape.

CONFIRMED (2026-09): realtyapi_bypolygon_state.py's flattened statewide
output (a bare JSON array, not the {message,...,searchResults} wrapper) uses
the identical per-record schema as /search/byzip -- checked field-by-field
via --inspect against a real MA statewide run. No mapping changes needed for
that source.

INPUT SHAPE: a file or directory of files, each holding one saved RealtyAPI
/search/* response object: {"message", "source", "total", "nextPage",
"resultCount", "searchResults": [...]}. A bare JSON array of listing objects
(already unwrapped) is also accepted.

Requires: psycopg2 (pip install psycopg2-binary), requests (only for --fetch
mode), and this file living next to load_listings.py (imports upsert_listings
and _check_schema_exists from it).

Usage:
  # Load from already-saved RealtyAPI response file(s)/directory:
  python load_listings_realtyapi.py realtyapi_data/ --dsn "postgresql://..."

  # Inspect one file's shape without loading anything:
  python load_listings_realtyapi.py realtyapi_data/stoneham.json --inspect

  # Fetch directly from the API for a zip and load (requires --api-key):
  python load_listings_realtyapi.py --fetch-zip 02180 --api-key "$REALTYAPI_KEY" --dsn "postgresql://..."

  # Load ALL page file(s) from one scoped run, then mark any RealtyAPI
  # listing in that same scope that did NOT appear as removed (status=
  # 'Inactive', removed_date=today). Scope must match what was actually
  # fetched -- never something wider -- and must include every page file
  # from the run, or later pages get wrongly marked removed:
  python load_listings_realtyapi.py realtyapi_data/nh_lincoln_page1.json \
      --scope-city Lincoln --scope-state NH --mark-removed --dsn "postgresql://..."
  python load_listings_realtyapi.py realtyapi_data/realtyapi_zip02180_page1.json \
      --scope-zip 02180 --mark-removed --dsn "postgresql://..."

  # State-wide mark-removed (NEW) -- only for a fetch that covers the WHOLE
  # state in one run (e.g. realtyapi_bypolygon_state.py's output), and only
  # after you've verified that fetch was genuinely complete (no dropped
  # polygon parts, no silent 10k-per-part pagination cap -- see that
  # script's docstring). --confirm-complete-fetch is a manual acknowledgment,
  # not an automated check -- it does not verify anything itself:
  python load_listings_realtyapi.py realtyapi-data/realtyapi_ma.json \
      --scope-state MA --scope-state-only --confirm-complete-fetch \
      --mark-removed --dsn "postgresql://..."
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import date

# Reuse the DB write path and schema check -- do not duplicate the SQL here.
from listings_db import upsert_listings, _check_schema_exists, _iso_date_only, _expand_paths

REALTYAPI_BASE = "https://realtor.realtyapi.io"

# Confirmed wrapper key for /search/byzip and (presumably) the other
# /search/* endpoints. Kept as a list with fallbacks in case /search/
# bylocation or /search/bycoordinates ever wrap it differently.
RESULT_LIST_KEYS = ["searchResults", "results", "listings", "properties"]

# RealtyAPI/Realtor.com value -> RentCast-style value, so the shared
# `listings` table has one vocabulary across providers regardless of
# `source`. Add to these as new values show up (e.g. "condo", "multi_family").
PROPERTY_TYPE_MAP = {
    "single_family": "Single Family",
    "land": "Land",
    "condo": "Condo",
    "condos": "Condo",
    "multi_family": "Multi-Family",
    "townhomes": "Townhouse",
    "mobile": "Mobile/Manufactured",
    "farm": "Farm",
    "coop": "Co-op",
    "duplex_triplex": "Duplex/Triplex",
}
STATUS_MAP = {
    "for_sale": "Active",
    "ready_to_build": "Active",  # new-construction/builder listings -- genuinely active inventory, just pre-MLS
    "pending": "Pending",
    "sold": "Sold",
    "off_market": "Inactive",
}

# CONFIRMED real-data bug fix (2026-08-25): RealtyAPI's top-level `status`
# field can be STALE/WRONG relative to `flags` -- a real record was seen
# with status="for_sale" AND flags.is_pending=true simultaneously; the
# listing genuinely was pending on realtor.com. `flags` carries more
# precise state than `status` for at least this case, so it takes
# priority when present. Same pattern already existed for
# flags.is_new_construction (see listing_type below) -- this extends the
# same idea to status itself.
#
# ONLY confirmed flag so far is is_pending. No real sample has shown an
# is_contingent (or similarly-named) flag yet -- do NOT guess a key name
# here; _listing_dict_from_raw below surfaces any unrecognized true flag
# so a future one gets caught rather than silently ignored the way
# is_pending was until this fix.
# CONFIRMED 2026-08-25 from the real 12-file MD/MA/NJ corpus: is_pending
# and is_contingent are real transaction-stage flags -- a listing marked
# either one is genuinely not available the way an Active listing is.
STATUS_FLAG_OVERRIDES = {
    "is_pending": "Pending",
    "is_contingent": "Contingent",
}
# is_coming_soon (CONFIRMED real, ~30+ hits in the same run) is
# DELIBERATELY treated as Active, not its own status -- explicit decision
# to upload Coming Soon listings as Active rather than a distinct bucket.
# is_foreclosure (CONFIRMED real, 3 hits) is handled separately below via
# listing_type, NOT here -- it's a different KIND of signal than
# Pending/Contingent: those describe the CURRENT TRANSACTION STAGE
# (mutually exclusive with Active); foreclosure describes WHY the
# property is for sale, and isn't mutually exclusive with any status -- a
# foreclosure listing can itself be Active, Pending, etc. Forcing it into
# `status` here would incorrectly treat "this is a foreclosure" as if it
# meant "this isn't really for sale," the same mistake this whole fix
# started out correcting.
LISTING_TYPE_FLAGS = {
    "is_foreclosure": "Foreclosure",
}
# Flags known to be unrelated to status (either irrelevant, deliberately
# treated as Active, or handled via listing_type instead -- see
# LISTING_TYPE_FLAGS), so they don't trigger the "unrecognized flag"
# warning below.
NON_STATUS_FLAGS = {"is_new_construction", "is_new_listing", "is_price_reduced",
                     "is_foreclosure", "is_coming_soon"}


def _get(d, path, default=None):
    """Dotted-path getter that fails soft instead of raising."""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _listing_type_from_flags(flags: dict) -> str | None:
    """New construction takes priority if a record somehow had both flags
    true (not seen in practice, but this keeps behavior deterministic
    rather than depending on dict key order)."""
    if flags.get("is_new_construction"):
        return "New Construction"
    if flags.get("is_foreclosure"):
        return "Foreclosure"
    return None


def _to_float(val):
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_records(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in RESULT_LIST_KEYS:
            val = payload.get(key)
            if isinstance(val, list):
                return val
        raise ValueError(
            f"Couldn't find a listing array under any of {RESULT_LIST_KEYS} "
            f"(top-level keys were: {list(payload.keys())}). Run with "
            f"--inspect and adjust RESULT_LIST_KEYS."
        )
    raise ValueError(f"Unrecognized payload type: {type(payload)}")


def _listing_dict_from_raw(raw: dict) -> dict:
    """Maps one RealtyAPI /search/byzip listing object into a row dict
    matching the listings table's column names. Confirmed against a real
    Stoneham, MA sample response -- see module docstring."""
    address = raw.get("address") or {}
    advertisers = raw.get("advertisers")
    flags = raw.get("flags") or {}

    property_type_raw = raw.get("property_type")
    status_raw = raw.get("status")

    # flags takes priority over the raw status string -- see
    # STATUS_FLAG_OVERRIDES' comment above for the confirmed real case
    # this fixes (status="for_sale" + flags.is_pending=true on the same
    # record).
    status_from_flags = None
    for flag_key, mapped_status in STATUS_FLAG_OVERRIDES.items():
        if flags.get(flag_key):
            status_from_flags = mapped_status
            break
    final_status = status_from_flags or STATUS_MAP.get(status_raw, status_raw)

    unhandled_true_flags = [
        k for k, v in flags.items()
        if v is True and k not in STATUS_FLAG_OVERRIDES and k not in NON_STATUS_FLAGS
    ]
    if unhandled_true_flags:
        listing_ref = raw.get("listing_id") or raw.get("property_id") or "<unknown>"
        print(f"  NOTE: listing {listing_ref}: unrecognized true flag(s) {unhandled_true_flags} "
              f"-- check if this should affect status (see STATUS_FLAG_OVERRIDES)")

    line = address.get("line")
    city = address.get("city")
    state_code = address.get("state_code")
    postal_code = address.get("postal_code")
    formatted_address = ", ".join(
        part for part in [line, city, f"{state_code} {postal_code}".strip()] if part
    ) or None

    return {
        "listing_id": raw.get("listing_id") or raw.get("property_id"),  # falls back to property_id for new-construction/builder listings (status "ready_to_build" etc.), which carry no MLS listing_id at all -- still Realtor's own raw id either way, just from a different one of their id fields
        "formatted_address": formatted_address,
        "address_line_1": line,
        "address_line_2": address.get("unit"),
        "city": city,
        "state": state_code,
        "zip_code": postal_code,
        "county": raw.get("county"),
        "latitude": address.get("latitude"),
        "longitude": address.get("longitude"),
        "property_type": PROPERTY_TYPE_MAP.get(property_type_raw, property_type_raw),
        "bedrooms": raw.get("beds"),
        "bathrooms": _to_float(raw.get("baths")),
        "square_footage": raw.get("sqft"),
        "lot_size": raw.get("lot_sqft"),  # NOTE: confirm units (sqft) match property_values.acreage before comparing -- see listings_schema.sql
        "year_built": None,  # NOT returned by /search/byzip -- only available via /details/byid (1 credit/call extra)
        "status": final_status,
        "price": raw.get("list_price"),
        "listing_type": _listing_type_from_flags(flags),
        "listed_date": _iso_date_only(raw.get("list_date")),
        "removed_date": None,  # Search results are active listings; removed_date isn't populated here
        "days_on_market": None,  # NOT returned by /search/byzip
        "mls_name": None,  # NOT returned by /search/byzip -- only via /details/byid
        "mls_number": None,  # NOT returned by /search/byzip -- only via /details/byid
        "agent": json.dumps(advertisers) if advertisers is not None else None,
        "office": None,  # office is a plain string nested inside each advertiser, not a separate object -- see `agent`
        "price_history": None,  # NOT returned by /search/byzip
        "source": "RealtyAPI-Realtor",
    }
    # Fields RealtyAPI returns that have no column in the current listings
    # schema, dropped here rather than guessed into an unrelated column:
    # href (listing URL), primary_photo/photos, estimate (Realtor.com AVM),
    # last_sold_price/last_sold_date, price_reduced_amount/date,
    # flags.is_new_listing, source_type, has_specials, virtual_tours.
    # Add columns first if you want to keep any of these.


def parse_listings_file(path: str) -> list[dict]:
    with open(path) as f:
        payload = json.load(f)
    records = _extract_records(payload)
    return [_listing_dict_from_raw(r) for r in records]


def inspect_file(path: str):
    with open(path) as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        print(f"total: {payload.get('total')}  nextPage: {payload.get('nextPage')}  resultCount: {payload.get('resultCount')}")
    records = _extract_records(payload)
    print(f"{path}: found {len(records)} record(s)")
    if not records:
        return
    sample = records[0]
    print("\nTop-level keys of first record:")
    print(f"  {sorted(sample.keys())}")
    print("\nMapped row:")
    row = _listing_dict_from_raw(sample)
    for k, v in row.items():
        flag = "  <-- NULL" if v in (None, "") else ""
        print(f"  {k}: {v!r}{flag}")


def fetch_search_byzip(zip_code: str, api_key: str, property_type: str = "single_family,land", max_pages: int = 5) -> list[dict]:
    """Live-fetch path. RealtyAPI paginates via `page` + a boolean `nextPage`
    flag in the response (not a total-page count), so we just keep bumping
    `page` until nextPage is falsy or max_pages is hit."""
    import requests

    all_records = []
    page = 1
    while page <= max_pages:
        resp = requests.get(
            f"{REALTYAPI_BASE}/search/byzip",
            headers={"x-realtyapi-key": api_key},
            params={"zipCode": zip_code, "propertyType": property_type, "resultCount": 200, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        records = _extract_records(payload)
        all_records.extend(records)
        if page == 1:
            print(f"  zip {zip_code}: total={payload.get('total')}")
        if not payload.get("nextPage"):
            break
        page += 1
    print(f"  fetched {len(all_records)} record(s) for zip {zip_code} across {page} page(s)")
    return [_listing_dict_from_raw(r) for r in all_records]


def mark_removed_listings(conn, listing_ids: list[str], scope_city: str | None = None,
                           scope_state: str | None = None, scope_zip: str | None = None,
                           scope_state_only: bool = False) -> int:
    """Marks any currently-non-Inactive RealtyAPI listing IN THE GIVEN SCOPE
    that is NOT in listing_ids as removed (status='Inactive', removed_date=today).

    Scope must match exactly what was just fetched -- (city, state), zip, or
    (NEW) the whole state via scope_state_only -- never wider than what was
    actually re-downloaded. Source is always pinned to 'RealtyAPI-Realtor' so
    this never touches RentCast-sourced rows, which this fetch says nothing
    about.

    scope_state_only=True is for a run whose fetch genuinely covered the
    ENTIRE state in one go (e.g. realtyapi_bypolygon_state.py's output),
    unlike the original city/zip scopes, which assumed a run only ever
    covered a small slice of a state. This has a much larger blast radius --
    every non-Inactive RealtyAPI-Realtor row in that state not present in
    listing_ids gets marked removed. The CLI layer (see main()) requires
    --confirm-complete-fetch alongside --scope-state-only for exactly this
    reason; this function itself does not re-verify fetch completeness --
    that verification (no dropped polygon parts, no silent pagination cap)
    has to happen before this is called.

    Marks rather than deletes: gap_results.listing_id has a plain FK to
    listings.listing_id with no ON DELETE clause, so a hard DELETE here would
    throw on any listing that's already been gap-computed. Marking preserves
    history and keeps gap_results intact.

    listing_ids should be the FULL set from every page of the current run --
    passing only page 1 of a multi-page fetch would wrongly mark real,
    still-active listings from later pages as removed.
    """
    if scope_zip:
        where_scope = "zip_code = %s"
        scope_params = [scope_zip]
    elif scope_state_only:
        where_scope = "state = %s"
        scope_params = [scope_state]
    elif scope_city and scope_state:
        where_scope = "city = %s AND state = %s"
        scope_params = [scope_city, scope_state]
    else:
        raise ValueError("mark_removed_listings requires scope_zip, scope_state_only "
                          "(+ scope_state), or both scope_city and scope_state")

    query = f"""
        UPDATE listings
        SET status = 'Inactive', removed_date = CURRENT_DATE
        WHERE source = 'RealtyAPI-Realtor'
          AND {where_scope}
          AND status != 'Inactive'
          AND listing_id != ALL(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, scope_params + [listing_ids])
        n = cur.rowcount
    conn.commit()
    return n


def main():
    if len(sys.argv) == 1:
        print("Usage:")
        print("  python load_listings_realtyapi.py <file-or-directory> --dsn \"<postgresql-dsn>\"")
        print("  python load_listings_realtyapi.py <file> --inspect")
        print("  python load_listings_realtyapi.py --fetch-zip 02180 --api-key KEY --dsn \"<postgresql-dsn>\"")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", help="RealtyAPI response JSON file(s) and/or directory/directories (non-recursive)")
    parser.add_argument("--dsn", help="Postgres DSN (required unless --inspect)")
    parser.add_argument("--inspect", action="store_true", help="Print the shape of the first file's first record and exit -- no DB write")
    parser.add_argument("--fetch-zip", help="Live-fetch listings for this ZIP via /search/byzip instead of reading files")
    parser.add_argument("--api-key", help="RealtyAPI key (required with --fetch-zip)")
    parser.add_argument("--mark-removed", action="store_true",
                         help="After upserting, mark any RealtyAPI listing in the given scope that "
                              "did NOT appear in this run as Inactive/removed_date=today. Requires "
                              "--scope-zip, both --scope-city and --scope-state, or "
                              "--scope-state-only + --scope-state (+ --confirm-complete-fetch) -- "
                              "must match the scope that was actually fetched, never something wider.")
    parser.add_argument("--scope-city", help="City scope for --mark-removed (pair with --scope-state)")
    parser.add_argument("--scope-state", help="State scope for --mark-removed (pair with --scope-city, "
                                               "or alone with --scope-state-only)")
    parser.add_argument("--scope-zip", help="Zip scope for --mark-removed")
    parser.add_argument("--scope-state-only", action="store_true",
                         help="Use --scope-state alone (no city) for --mark-removed, marking removed "
                              "listings across the WHOLE state. Only safe for a fetch that genuinely "
                              "covered the entire state in one run (e.g. realtyapi_bypolygon_state.py's "
                              "output) -- requires --confirm-complete-fetch, since this has a much "
                              "larger blast radius than the city/zip-scoped modes.")
    parser.add_argument("--confirm-complete-fetch", action="store_true",
                         help="Required alongside --scope-state-only. Manually acknowledges you've "
                              "verified this fetch covers the whole state with nothing silently "
                              "dropped or capped (no missing polygon parts, no silent 10k-per-part "
                              "pagination cap -- see realtyapi_bypolygon_state.py's docstring). This "
                              "flag does not verify anything itself.")
    args = parser.parse_args()

    if args.mark_removed:
        has_zip_scope = bool(args.scope_zip)
        has_city_scope = bool(args.scope_city and args.scope_state)
        has_state_only_scope = bool(args.scope_state_only and args.scope_state and not args.scope_city)

        if args.scope_state_only and not has_state_only_scope:
            print("ERROR: --scope-state-only requires --scope-state and must NOT be combined with "
                  "--scope-city (use --scope-city + --scope-state without --scope-state-only instead).")
            sys.exit(1)

        if has_state_only_scope and not args.confirm_complete_fetch:
            print("ERROR: --scope-state-only requires --confirm-complete-fetch -- this marks removed "
                  "listings across the ENTIRE state, only safe if you've verified this fetch had no "
                  "dropped polygon parts and didn't hit a silent pagination cap.")
            sys.exit(1)

        scope_count = sum([has_zip_scope, has_city_scope, has_state_only_scope])
        if scope_count != 1:
            print("ERROR: --mark-removed requires exactly one scope: --scope-zip, "
                  "--scope-city + --scope-state, or --scope-state-only + --scope-state "
                  "(+ --confirm-complete-fetch).")
            sys.exit(1)

    if args.inspect:
        paths = _expand_paths(args.files)
        if not paths:
            print("ERROR: --inspect needs at least one file.")
            sys.exit(1)
        inspect_file(paths[0])
        return

    if args.fetch_zip:
        if not args.api_key or not args.dsn:
            print("ERROR: --fetch-zip requires both --api-key and --dsn.")
            sys.exit(1)
        listings = fetch_search_byzip(args.fetch_zip, args.api_key)
        import psycopg2
        conn = psycopg2.connect(args.dsn)
        try:
            _check_schema_exists(conn)
            n = upsert_listings(conn, listings)
            print(f"Done. {n} listings upserted for zip {args.fetch_zip}.")
            if args.mark_removed:
                ids = [l["listing_id"] for l in listings if l.get("listing_id")]
                n_removed = mark_removed_listings(conn, ids, scope_zip=args.fetch_zip)
                print(f"Marked {n_removed} listing(s) in zip {args.fetch_zip} as removed "
                      f"(not present in this fetch).")
        finally:
            conn.close()
        return

    if not args.dsn:
        print("ERROR: --dsn is required (or use --inspect).")
        sys.exit(1)

    paths = _expand_paths(args.files)
    if not paths:
        print("ERROR: no .json/.geojson files found across the given path(s).")
        sys.exit(1)

    print(f"Loading {len(paths)} file(s)...")

    import psycopg2
    conn = psycopg2.connect(args.dsn)
    try:
        _check_schema_exists(conn)
        total = 0
        all_ids = []  # full set across every file THIS run -- required for correct --mark-removed
        for path in paths:
            listings = parse_listings_file(path)
            n = upsert_listings(conn, listings)
            print(f"  {path}: upserted {n} listings")
            total += n
            all_ids.extend(l["listing_id"] for l in listings if l.get("listing_id"))
        print(f"\nDone. {total} total listings upserted across {len(paths)} file(s).")

        if args.mark_removed:
            n_removed = mark_removed_listings(
                conn, all_ids,
                scope_city=args.scope_city, scope_state=args.scope_state, scope_zip=args.scope_zip,
                scope_state_only=args.scope_state_only,
            )
            if args.scope_zip:
                scope_label = args.scope_zip
            elif args.scope_state_only:
                scope_label = f"{args.scope_state} (state-wide)"
            else:
                scope_label = f"{args.scope_city}, {args.scope_state}"
            print(f"Marked {n_removed} listing(s) in {scope_label} as removed "
                  f"(not present across the {len(paths)} file(s) loaded this run).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()