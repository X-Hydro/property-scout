"""
Shared RealtyAPI response-parsing/normalization helpers -- deliberately
DEPENDENCY-FREE (no psycopg2, no listings_db.py) so anything that just
needs to read a RealtyAPI response file (select_offmarket_zips.py,
nh_spider.py's offmarket path) doesn't have to pull in
load_listings_realtyapi.py's DB-writing machinery just to parse JSON.

load_listings_realtyapi.py has its OWN copy of this same logic, kept
deliberately separate rather than refactored to import from here, to
avoid re-touching an already-deployed, working file. If the two ever
drift apart, treat this one as the source of truth for anything that
doesn't also need DB writes -- and treat load_listings_realtyapi.py's
copy as the source of truth for the actual `listings` table column
mapping, since that one also handles fields (agent JSON, mls_name, etc.)
this shared version doesn't need to.

Usage:
    from realtyapi_response_utils import extract_records, listing_dict_from_raw
    records = extract_records(json.load(open(path)))
    rows = [listing_dict_from_raw(r) for r in records]
"""

RESULT_LIST_KEYS = ["searchResults", "results", "listings", "properties"]

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
    "ready_to_build": "Active",
    "pending": "Pending",
    "sold": "Sold",
    "off_market": "Inactive",
}
STATUS_FLAG_OVERRIDES = {
    "is_pending": "Pending",
    "is_contingent": "Contingent",
}
NON_STATUS_FLAGS = {"is_new_construction", "is_new_listing", "is_price_reduced",
                     "is_foreclosure", "is_coming_soon"}


def extract_records(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in RESULT_LIST_KEYS:
            val = payload.get(key)
            if isinstance(val, list):
                return val
        raise ValueError(
            f"Couldn't find a listing array under any of {RESULT_LIST_KEYS} "
            f"(top-level keys were: {list(payload.keys())})."
        )
    raise ValueError(f"Unrecognized payload type: {type(payload)}")


def listing_status_and_type(raw: dict) -> tuple[str, str | None]:
    """Returns (status, listing_type), applying the same flags-take-priority
    logic as load_listings_realtyapi.py's _listing_dict_from_raw (see that
    file for the confirmed real-data bug this fixes: status can be stale
    relative to flags.is_pending)."""
    flags = raw.get("flags") or {}
    status_raw = raw.get("status")

    status_from_flags = None
    for flag_key, mapped_status in STATUS_FLAG_OVERRIDES.items():
        if flags.get(flag_key):
            status_from_flags = mapped_status
            break
    status = status_from_flags or STATUS_MAP.get(status_raw, status_raw)

    listing_type = None
    if flags.get("is_new_construction"):
        listing_type = "New Construction"
    elif flags.get("is_foreclosure"):
        listing_type = "Foreclosure"

    return status, listing_type


def listing_dict_from_raw(raw: dict) -> dict:
    """Minimal mapped row -- just the fields select_offmarket_zips.py and
    nh_spider.py actually need (status, property_type, zip_code, lat/lon).
    NOT a full listings-table row (no address text, agent JSON, mls
    fields, etc. -- see load_listings_realtyapi.py for the complete
    mapping used for actual DB writes)."""
    address = raw.get("address") or {}
    status, listing_type = listing_status_and_type(raw)
    property_type_raw = raw.get("property_type")

    return {
        "status": status,
        "listing_type": listing_type,
        "property_type": PROPERTY_TYPE_MAP.get(property_type_raw, property_type_raw),
        "zip_code": address.get("postal_code"),
        "latitude": address.get("latitude"),
        "longitude": address.get("longitude"),
    }