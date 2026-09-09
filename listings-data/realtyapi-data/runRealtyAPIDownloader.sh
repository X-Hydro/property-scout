#!/bin/bash

#export REALTYAPI_KEY="X"

set -e

if [ -z "$REALTYAPI_KEY" ]; then
    echo "Error: REALTYAPI_KEY is not set." >&2
    echo "Please set it before running this script." >&2
    exit 1
fi

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 <state_abbr> <city>"
    echo "       $0 --zip <zipcode>"
    echo
    echo "Examples:"
    echo "  $0 MA Stoneham         # single city -- CONFIRMED working"
    echo "  $0 --zip 02180         # single zip -- CONFIRMED working"
    echo
    echo "Downloads active Single Family + Land listings from RealtyAPI's"
    echo "Realtor.com endpoint."
    echo
    echo "*** STATEWIDE (state only, no city) IS NOT SUPPORTED -- CONFIRMED BROKEN ***"
    echo "Tested 2026-08-25: '\$0 NJ' returned message=Success, total=168, with zero"
    echo "error -- but every result clustered in 8 neighboring Bergen/Passaic County"
    echo "towns, nothing from the rest of the state. /search/bylocation appears to"
    echo "geocode 'location' to a single POINT and search a radius around it, not"
    echo "match a state boundary -- a bare state abbreviation is a bad geocoding"
    echo "input and silently returns a small, misleadingly plausible-looking subset"
    echo "instead of an error. This script now REQUIRES a city so that mistake can't"
    echo "happen by accident -- always pass one, or use --zip."
    echo
    echo "Realistic statewide coverage likely isn't achievable through this endpoint"
    echo "at all (nothing found resolves a state boundary) -- and even if it were,"
    echo "a real state's worth of listings would need far more than your 250/month"
    echo "shared quota at resultCount=200 per request. City-by-city (or zip-by-zip)"
    echo "is both the only proven-accurate scope and the only realistic one."
    echo
    echo "Uses resultCount=200 (the confirmed max) on every request -- billing is"
    echo "per REQUEST, not per record, so there's no cost to always asking for the"
    echo "max page size."
    echo
    echo "NOTE: RealtyAPI's free tier (250 requests/month) is a SHARED pool across"
    echo "ALL providers on your account (Realtor, Redfin, Homes.com, etc.), not"
    echo "per-endpoint -- factor in any testing already done this month. Override"
    echo "the default cap with REALTYAPI_QUOTA_WARNING if you know your real"
    echo "remaining balance, e.g.:"
    echo "  REALTYAPI_QUOTA_WARNING=40 $0 MA Stoneham"
    exit 1
fi

RESULT_COUNT=200
PROPERTY_TYPE="single_family,land"
QUOTA_WARNING_THRESHOLD="${REALTYAPI_QUOTA_WARNING:-250}"   # shared account-wide pool -- see usage note above

# Pulls a top-level scalar field out of a saved JSON response body.
# RealtyAPI puts pagination info (total/nextPage/resultCount) in the BODY,
# not response headers like RentCast does -- so this replaces the
# grep-on-headers-file approach from runRentCastMLSDownloader.sh.
json_field() {
    local file="$1" field="$2"
    python3 -c "
import json
with open('$file') as f:
    d = json.load(f)
v = d.get('$field')
print(v if v is not None else '')
"
}

if [ "$1" == "--zip" ]; then
    # CONFIRMED path -- matches the curl that was manually tested against
    # a real Stoneham, MA response.
    ZIP="$2"
    if [ -z "$ZIP" ]; then
        echo "Error: --zip requires a zip code, e.g. $0 --zip 02180" >&2
        exit 1
    fi
    DATA_DIR="realtyapi_data"
    mkdir -p "$DATA_DIR"
    FILE_PREFIX="realtyapi_zip${ZIP}"
    SCOPE_LABEL="zip ${ZIP}"

    fetch_page() {
        local page_num="$1"
        local out_file="${DATA_DIR}/${FILE_PREFIX}_page${page_num}.json"
        curl -s "https://realtor.realtyapi.io/search/byzip" \
            -H "x-realtyapi-key: $REALTYAPI_KEY" \
            -G \
            --data-urlencode "zipCode=${ZIP}" \
            --data-urlencode "propertyType=${PROPERTY_TYPE}" \
            --data-urlencode "resultCount=${RESULT_COUNT}" \
            --data-urlencode "page=${page_num}" \
            -o "$out_file"
        echo "$out_file"
    }
else
    # bylocation path -- REQUIRES a city (see usage text above for why:
    # state-only was confirmed to silently return a small radius-search
    # subset, not real statewide coverage).
    STATE=$(echo "$1" | tr '[:lower:]' '[:upper:]')
    CITY="$2"
    if [ -z "$CITY" ]; then
        echo "ERROR: a city is required -- '\$0 ${STATE}' alone is not supported." >&2
        echo "CONFIRMED 2026-08-25: state-only /search/bylocation silently returns a" >&2
        echo "small radius-search subset around one geocoded point, not the whole" >&2
        echo "state, with no error to indicate anything's wrong. Pass a city, e.g." >&2
        echo "  $0 ${STATE} <city>" >&2
        echo "or use --zip for a specific zip code instead." >&2
        exit 1
    fi
    LOCATION="${CITY}, ${STATE}"
    DATA_DIR="realtyapi_data"
    mkdir -p "$DATA_DIR"
    FILE_PREFIX=$(echo "${STATE}_${CITY}" | tr '[:upper:] ' '[:lower:]_' | sed 's/_$//')
    SCOPE_LABEL="$LOCATION"

    fetch_page() {
        local page_num="$1"
        local out_file="${DATA_DIR}/${FILE_PREFIX}_page${page_num}.json"
        curl -s "https://realtor.realtyapi.io/search/bylocation" \
            -H "x-realtyapi-key: $REALTYAPI_KEY" \
            -G \
            --data-urlencode "location=${LOCATION}" \
            --data-urlencode "propertyType=${PROPERTY_TYPE}" \
            --data-urlencode "resultCount=${RESULT_COUNT}" \
            --data-urlencode "page=${page_num}" \
            -o "$out_file"
        echo "$out_file"
    }
fi

echo "Fetching page 1 for ${SCOPE_LABEL}..."
PAGE1_FILE=$(fetch_page 1)
PAGE_FILES=("$PAGE1_FILE")

MESSAGE=$(json_field "$PAGE1_FILE" message)
echo "  message: ${MESSAGE}"

if [[ "$MESSAGE" != Success* ]]; then
    echo "" >&2
    echo "ERROR: response message doesn't start with 'Success' -- something's" >&2
    echo "wrong (bad param, unresolved location, etc). Check ${PAGE1_FILE} directly" >&2
    echo "before running this again." >&2
    exit 1
fi

TOTAL=$(json_field "$PAGE1_FILE" total)
NEXTPAGE=$(json_field "$PAGE1_FILE" nextPage)
RESULT_COUNT_ACTUAL=$(json_field "$PAGE1_FILE" resultCount)

if [ -z "$TOTAL" ]; then
    echo "Warning: could not read 'total' from ${PAGE1_FILE} -- inspect it directly." >&2
    exit 1
fi

echo "  ${SCOPE_LABEL}: ${TOTAL} total listings (page 1 returned ${RESULT_COUNT_ACTUAL})"

PAGES_NEEDED=$(( (TOTAL + RESULT_COUNT - 1) / RESULT_COUNT ))
echo "  -> ${PAGES_NEEDED} request(s) needed at resultCount=${RESULT_COUNT} (1 already made)"

PAGES_TO_FETCH=$PAGES_NEEDED
if [ "$PAGES_NEEDED" -gt "$QUOTA_WARNING_THRESHOLD" ]; then
    PAGES_TO_FETCH=$QUOTA_WARNING_THRESHOLD
    CAPPED_TOTAL=$((RESULT_COUNT * QUOTA_WARNING_THRESHOLD))
    echo "" >&2
    echo "WARNING: ${PAGES_NEEDED} request(s) would be needed for all ${TOTAL} listings," >&2
    echo "exceeding your assumed ${QUOTA_WARNING_THRESHOLD}-request budget (shared across" >&2
    echo "all RealtyAPI providers this month -- override with REALTYAPI_QUOTA_WARNING)." >&2
    echo "Capping at ${QUOTA_WARNING_THRESHOLD} request(s) (~${CAPPED_TOTAL} listings) instead." >&2
fi

PAGE=2
CONTINUE="$NEXTPAGE"
while [ "$CONTINUE" == "True" ] && [ "$PAGE" -le "$PAGES_TO_FETCH" ]; do
    echo "Fetching page ${PAGE}..."
    OUT_FILE=$(fetch_page "$PAGE")
    MSG=$(json_field "$OUT_FILE" message)
    if [[ "$MSG" != Success* ]]; then
        echo "WARNING: page ${PAGE} response wasn't a Success -- stopping early. Check ${OUT_FILE}." >&2
        break
    fi
    PAGE_FILES+=("$OUT_FILE")
    CONTINUE=$(json_field "$OUT_FILE" nextPage)
    PAGE=$((PAGE + 1))
done

PAGES_FETCHED=$((PAGE - 1))
echo ""
echo "Done: ${DATA_DIR}/ now has ${PAGES_FETCHED} page file(s) for ${SCOPE_LABEL}."
echo "Used ${PAGES_FETCHED} API request(s) out of your shared RealtyAPI monthly quota."
if [ "$PAGES_FETCHED" -lt "$PAGES_NEEDED" ]; then
    echo "NOTE: stopped before covering all ${TOTAL} listings -- ${PAGES_NEEDED} page(s) would" >&2
    echo "be needed in total. Re-run with a higher REALTYAPI_QUOTA_WARNING if you have quota left." >&2
    echo "NOTE: skipping auto-load below -- marking removed listings against a partial page set" >&2
    echo "would wrongly mark real, still-active listings from the unfetched pages as removed." >&2
    exit 0
fi

# Auto-load + mark-removed, scoped to exactly what this run fetched. Requires
# PROPERTYSCOUT_DSN so this stays optional -- unset it to just download files.
LOADER_SCRIPT="$(dirname "$0")/load_listings_realtyapi.py"
if [ "$1" == "--zip" ]; then
    SCOPE_ARGS=(--scope-zip "$ZIP")
else
    SCOPE_ARGS=(--scope-city "$CITY" --scope-state "$STATE")
fi

echo ""
if [ -n "$PROPERTYSCOUT_DSN" ]; then
    echo "Loading ${PAGES_FETCHED} page file(s) into the DB and marking removed listings for ${SCOPE_LABEL}..."
    python "$LOADER_SCRIPT" "${PAGE_FILES[@]}" "${SCOPE_ARGS[@]}" --mark-removed --dsn "$PROPERTYSCOUT_DSN"
else
    echo "PROPERTYSCOUT_DSN not set -- skipping auto-load. Load + mark-removed manually with:"
    echo "  python \"$LOADER_SCRIPT\" ${PAGE_FILES[*]} ${SCOPE_ARGS[*]} --mark-removed --dsn \"<postgresql-dsn>\""
fi