#!/bin/bash

#export RENTCAST_API_KEY="X"

set -e

if [ -z "$RENTCAST_API_KEY" ]; then
    echo "Error: RENTCAST_API_KEY is not set." >&2
    echo "Please set it before running this script." >&2
    exit 1
fi

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 <state_abbr> [city]"
    echo
    echo "Examples:"
    echo "  $0 CT              # statewide"
    echo "  $0 CT Bristol      # single city"
    echo
    echo "Downloads ALL active Single Family + Land listings, using the fewest"
    echo "possible RentCast API requests:"
    echo "  - queries by state alone (or state+city if provided) -- no per-city"
    echo "    looping needed to cover a whole state"
    echo "  - limit=500 (RentCast's max) on every request -- billing is per"
    echo "    REQUEST, not per record returned, so there's no cost to always"
    echo "    asking for the max page size"
    echo "  - the first request also sets includeTotalCount=true, so the exact"
    echo "    number of additional requests needed is known BEFORE running them"
    exit 1
fi

STATE=$(echo "$1" | tr '[:lower:]' '[:upper:]')
STATE_LOWER=$(echo "$STATE" | tr '[:upper:]' '[:lower:]')
CITY="$2"   # empty string if not provided -- fetch_page treats that as "no city filter"

if [ -n "$CITY" ]; then
    CITY_FILE=$(echo "$CITY" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')
    DATA_DIR="${STATE_LOWER}_data"
    mkdir -p "$DATA_DIR"
    FILE_PREFIX="${STATE_LOWER}_${CITY_FILE}"
else
    DATA_DIR="${STATE_LOWER}_data"
    mkdir -p "$DATA_DIR"
    FILE_PREFIX="${STATE_LOWER}"
fi

LIMIT=500
QUOTA_WARNING_THRESHOLD=10   # your monthly free-tier request allowance

fetch_page() {
    local offset="$1"
    local page_num="$2"
    local include_total="$3"   # "true" or "false"
    local out_file="${DATA_DIR}/${FILE_PREFIX}_page${page_num}.json"
    local headers_file="${DATA_DIR}/${FILE_PREFIX}_page${page_num}_headers.txt"

    local curl_args=(
        -sG "https://api.rentcast.io/v1/listings/sale"
        --data-urlencode "state=${STATE}"
        --data-urlencode "status=Active"
        --data-urlencode "propertyType=Single Family|Land"
        --data-urlencode "limit=${LIMIT}"
        --data-urlencode "offset=${offset}"
        --data-urlencode "includeTotalCount=${include_total}"
        -H "accept: application/json"
        -H "X-Api-Key: $RENTCAST_API_KEY"
        -D "$headers_file"
        -o "$out_file"
    )
    if [ -n "$CITY" ]; then
        curl_args+=(--data-urlencode "city=${CITY}")
    fi
    curl "${curl_args[@]}"
}

echo "Fetching page 1 (offset=0, includeTotalCount=true)..."
fetch_page 0 1 true

TOTAL=$(grep -i '^x-total-count:' "${DATA_DIR}/${FILE_PREFIX}_page1_headers.txt" | tr -d '\r' | awk '{print $2}')

if [ -z "$TOTAL" ]; then
    echo "Warning: could not read X-Total-Count from the response headers --" >&2
    echo "check ${DATA_DIR}/${FILE_PREFIX}_page1_headers.txt directly." >&2
    exit 1
fi

PAGES_NEEDED=$(( (TOTAL + LIMIT - 1) / LIMIT ))

SCOPE_LABEL="${STATE}"
if [ -n "$CITY" ]; then
    SCOPE_LABEL="${CITY}, ${STATE}"
fi
echo "${SCOPE_LABEL}: ${TOTAL} total active Single Family|Land listings"
echo "  -> ${PAGES_NEEDED} request(s) needed at limit=${LIMIT} (1 already made)"

if [ "$PAGES_NEEDED" -gt "$QUOTA_WARNING_THRESHOLD" ]; then
    echo "" >&2
    echo "WARNING: ${PAGES_NEEDED} total requests exceeds your ${QUOTA_WARNING_THRESHOLD}/month" >&2
    echo "free-tier allowance. Stopping after page 1 -- page 1's data is still" >&2
    echo "saved in ${DATA_DIR}/. Narrow the query (e.g. drop Land, or add a" >&2
    echo "daysOld filter) or upgrade your plan before fetching the rest." >&2
    exit 1
fi

OFFSET=$LIMIT
PAGE=2
while [ "$OFFSET" -lt "$TOTAL" ]; do
    echo "Fetching page ${PAGE} (offset=${OFFSET})..."
    fetch_page "$OFFSET" "$PAGE" false
    OFFSET=$((OFFSET + LIMIT))
    PAGE=$((PAGE + 1))
done

echo ""
echo "Done: ${DATA_DIR}/ now has $((PAGE - 1)) page file(s), ${TOTAL} listings total."
echo "Used $((PAGE - 1)) API request(s) out of your monthly quota."