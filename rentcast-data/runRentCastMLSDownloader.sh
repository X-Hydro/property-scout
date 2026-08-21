#!/bin/bash

#export RENTCAST_API_KEY="X"

set -e

if [ -z "$RENTCAST_API_KEY" ]; then
    echo "Error: RENTCAST_API_KEY is not set." >&2
    echo "Please set it before running this script." >&2
    exit 1
fi

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <state_abbr> <city>"
    echo
    echo "Example:"
    echo "  $0 NH Lebanon"
    exit 1
fi

STATE=$(echo "$1" | tr '[:lower:]' '[:upper:]')
CITY="$2"

# Lowercase state and city for filenames/directories
STATE_LOWER=$(echo "$STATE" | tr '[:upper:]' '[:lower:]')
CITY_FILE=$(echo "$CITY" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')

# Create state data directory
DATA_DIR="${STATE_LOWER}_data"
mkdir -p "$DATA_DIR"

PREFIX="${DATA_DIR}/${STATE_LOWER}_${CITY_FILE}"

curl -sG "https://api.rentcast.io/v1/listings/sale" \
  --data-urlencode "city=${CITY}" \
  --data-urlencode "state=${STATE}" \
  --data-urlencode "status=Active" \
  --data-urlencode "propertyType=Single Family|Land" \
  --data-urlencode "limit=100" \
  --data-urlencode "includeTotalCount=true" \
  -H "accept: application/json" \
  -H "X-Api-Key: $RENTCAST_API_KEY" \
  -o "${PREFIX}_sfh_land.json"

echo "Saved:"
echo "  ${PREFIX}_headers.txt"
echo "  ${PREFIX}_sfh_land.json"