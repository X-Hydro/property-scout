"""
RealtyAPI /search/offmarket zip sweep.

Fetches Zillow off-market properties for a list of zip codes via
zillow.realtyapi.io/search/offmarket (zipCode input -- the original,
cheap mode; NOT the lat/lon/radius mode, which was evaluated and
rejected for statewide NH coverage due to call volume: 5,463 listings
would mean 5,463+ calls even with grid-dedup, vs. 245 zip calls. The
completeness caveat RealtyAPI support raised for large zips ("a thinned
selection spread across the zip rather than every parcel") is an
accepted, known risk of this cheaper path, not something this script
works around.

CONFIRMED: this endpoint has no pagination/total field -- each call is
a single request, one JSON response, done. Results are deterministic on
repeat calls (per RealtyAPI support). No retry-for-more-pages logic
needed, unlike realtyapi_bypolygon_state.py's listings fetch.

Atomic writes (<file>.partial renamed via os.replace() on success),
matching the convention already used elsewhere in this project (VGSI
scraper, history-replay CSVs) -- protects a partially-swept output
directory from corruption if the run is interrupted mid-zip.

Usage:
    python offmarket_sweep.py nh_offmarket_zips.txt ./offmarket-data --api-key $REALTYAPI_KEY
"""

import sys
import os
import json
import time
import argparse

import requests

from offmarket_to_geojson import payload_to_geojson

API_URL = "https://zillow.realtyapi.io/search/offmarket"
MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 2.0
REQUEST_SLEEP_SECONDS = 0.5


def sweep_zip(zip_code: str, api_key: str) -> dict:
    """Single call, with retry/backoff. No pagination to loop -- see
    module docstring."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                API_URL,
                headers={"x-realtyapi-key": api_key},
                params={"zipCode": zip_code},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not str(payload.get("message", "")).startswith("200"):
                raise ValueError(f"unexpected message: {payload.get('message')!r}")
            return payload
        except Exception as e:
            last_err = e
            print(f"    attempt {attempt}/{MAX_RETRIES} failed for zip {zip_code}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_SECONDS * attempt)
    raise RuntimeError(f"zip {zip_code}: all {MAX_RETRIES} attempts failed -- last error: {last_err}")


def sweep_zips(zip_codes: list[str], out_dir: str, api_key: str,
                debug_geojson_dir: str | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """
    Sweeps every zip, writing one file per zip under out_dir
    (nh_offmarket_zip<zip>.json). Returns (succeeded_zips, failed), where
    failed is [(zip, error_str), ...].

    debug_geojson_dir: if given, ALSO writes a QGIS-loadable point
    GeoJSON per zip (nh_offmarket_zip<zip>.geojson) -- deliberately a
    DIFFERENT directory from out_dir, never mixed in with the raw sweep
    files. This matters beyond just tidiness: join_parcels_offmarket.py's
    full-statewide-run mode globs every *.json file in out_dir as real
    sweep data (see its load_offmarket_points() docstring) -- a debug
    .geojson file sitting in that same directory wouldn't break that glob
    (different extension), but keeping them fully separate avoids ever
    having to reason about whether it might. Regenerated even on a
    cache-hit (raw file already existed, skipped the API call) if the
    debug file itself doesn't exist yet -- so turning on debug output
    later doesn't require deleting and re-sweeping already-fetched zips.

    A single zip's failure does not stop the rest -- matches the per-town
    failure isolation already used in run_spiders.py's main loop. Existing
    successfully-written files are left alone on a re-run (no re-fetch of
    zips already swept), so re-running against the same out_dir after a
    partial failure only re-does the failed ones if you pass just those.
    """
    os.makedirs(out_dir, exist_ok=True)
    if debug_geojson_dir:
        os.makedirs(debug_geojson_dir, exist_ok=True)
    succeeded = []
    failed = []

    for i, zip_code in enumerate(zip_codes, 1):
        out_path = os.path.join(out_dir, f"nh_offmarket_zip{zip_code}.json")
        partial_path = out_path + ".partial"
        debug_path = os.path.join(debug_geojson_dir, f"nh_offmarket_zip{zip_code}.geojson") \
            if debug_geojson_dir else None

        if os.path.exists(out_path):
            print(f"  [{i}/{len(zip_codes)}] zip {zip_code}: already swept, reusing "
                  f"{out_path} (delete it to force a re-fetch)")
            succeeded.append(zip_code)
            if debug_path and not os.path.exists(debug_path):
                with open(out_path) as f:
                    _write_debug_geojson(json.load(f), debug_path)
            continue

        print(f"  [{i}/{len(zip_codes)}] zip {zip_code}...")
        try:
            payload = sweep_zip(zip_code, api_key)
        except RuntimeError as e:
            print(f"    FAILED: {e}")
            failed.append((zip_code, str(e)))
            continue

        n = len(payload.get("offMarketResults", []))
        with open(partial_path, "w") as f:
            json.dump(payload, f)
        os.replace(partial_path, out_path)
        print(f"    {n} offmarket result(s) -> {out_path}")

        if debug_path:
            _write_debug_geojson(payload, debug_path)

        succeeded.append(zip_code)
        time.sleep(REQUEST_SLEEP_SECONDS)

    return succeeded, failed


def _write_debug_geojson(payload: dict, debug_path: str):
    geojson = payload_to_geojson(payload)
    debug_partial = debug_path + ".partial"
    with open(debug_partial, "w") as f:
        json.dump(geojson, f)
    os.replace(debug_partial, debug_path)
    print(f"    debug geojson ({len(geojson['features'])} point(s)) -> {debug_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("zips_file", help="text file, one zip code per line (e.g. from select_offmarket_zips.py)")
    parser.add_argument("out_dir", help="directory to write one JSON file per zip into")
    parser.add_argument("--api-key", default=os.environ.get("REALTYAPI_KEY"),
                         help="RealtyAPI key (default: $REALTYAPI_KEY)")
    parser.add_argument("--debug-geojson-dir",
                         help="also write a QGIS-loadable point .geojson per zip into this "
                              "directory (kept separate from out_dir -- see sweep_zips' docstring)")
    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: set REALTYAPI_KEY or pass --api-key.")
        sys.exit(1)

    with open(args.zips_file) as f:
        zip_codes = [line.strip() for line in f if line.strip()]
    print(f"Sweeping {len(zip_codes)} zip(s) -> {args.out_dir}")

    succeeded, failed = sweep_zips(zip_codes, args.out_dir, args.api_key,
                                    debug_geojson_dir=args.debug_geojson_dir)

    print(f"\nDone. {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        failed_path = os.path.join(args.out_dir, "offmarket_sweep_failed.txt")
        with open(failed_path, "w") as f:
            for z, err in failed:
                f.write(f"{z}\t{err}\n")
        print(f"See {failed_path} for failed zips -- re-run just those against this same "
              f"out_dir to retry (existing successful files are untouched).")
        sys.exit(1)


if __name__ == "__main__":
    main()