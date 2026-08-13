"""
Find abutters for every listing — ValueGap (Phase 1: find comparable candidates)

For each active listing (Land or Single Family only -- condos/multi-family
skipped per scope), resolves the listing to its own parcel using its
lat/lon (supplied directly by RentCast -- point-in-polygon against GRANIT's
boundaries), falling back to address matching only if the point doesn't
land inside any parcel, or discarding the match entirely if the resolved
parcel's size is wildly implausible vs. the listing's own reported lotSize
(catches cases like a brand-new subdivision lot resolving into the
underlying multi-thousand-acre land tract because the actual small lot
isn't in GRANIT yet). Neighbors come from three rules, combined:
  1. True geometric touching (any distance/street -- but still subject to
     the size/type filtering below, same as every other candidate)
  2. Within close_radius_m (default 100m) AND similar lot size to the
     target (default: within a 2.5x ratio) -- catches "purely adjacent by
     distance, different street name" cases, e.g. a parcel technically on
     South Peak Road that's actually right next to a Crooked Mtn Rd listing
  3. Within far_radius_m (default 250m) AND same normalized street name --
     catches "same street, further down" cases without relying on
     house-number sequence (which broke down around gaps/condos/unusual
     numbering)

This script's job is to find actual CANDIDATES -- not just anything nearby
for a human to sift through. Every candidate from any of the three rules
above is then filtered, uniformly:
  - Lot size must be comparable to the target (same lot_size_similar()
    check used above, applied here regardless of which rule found it --
    geometric touching and the 250m same-street rule don't check size on
    their own, so a 142-acre common-land parcel touching a 0.4-acre house
    lot is discarded here even though it "touches") -- EXCEPT when the
    target listing itself is Land: a land listing's own acreage isn't
    what matters for comparability, since the question is what the
    neighborhood supports (nearby built homes), not lot-size symmetry.
    A 1.11-acre vacant lot next to 0.35-acre built homes on the same
    small circle is genuinely comparable by proximity even though it
    fails a strict size ratio -- the size check is skipped for Land
    targets specifically, kept for Single Family targets.
  - Type must not be a KNOWN non-comparable land_use_desc (Commercial,
    Condo - No Land, Common Land, etc.). Unknown type (no VGSI match,
    land_use_desc is None) is NOT discarded -- we learned the hard way
    (the 'CROOKED MTN ROAD #101' case, which uses '#NNN' for a numbered
    land lot, not a condo unit despite looking like one) that guessing
    "probably not comparable" from incomplete data silently hides real
    candidates. Unknown-type survivors are tagged comp_eligible: None
    (unverified) rather than discarded, so you can still see and manually
    check them.
  - Blank address (nothing to identify the parcel by at all)

Output still includes land_use_desc and total_market_value for every
surviving candidate (for display/QC/styling), but this script does not
compute anything with them -- no gap, no ranking, no median. That's
compute_gap.py's job, which now also applies the same size/type filtering
independently, since it doesn't assume find_abutters.py's output is
pre-filtered by any particular version.

Inputs (three -- lincoln_joined.geojson is used for land_use_desc AND
total_market_value, i.e. both the assessor's plain-English property type
classification and the actual assessed dollar figure. Note: this script
still does NOT compute anything with the price data -- no gap, no median,
no ranking. It's included here purely so you can see/style/QC by value
while validating the neighbor set visually; the actual price *analysis*
(gap computation, ranking leads) still lives in compute_gap.py):
  - lincoln_nh.geojson       raw GRANIT parcels (geometry + address + type)
  - lincoln_joined.geojson   GRANIT+VGSI joined parcels -- land_use_desc + total_market_value
  - lincoln_sfh_land.json    RentCast active listings (Single Family + Land)

Output: three separate GeoJSON files, split by geometry type (a single
mixed-geometry FeatureCollection is valid GeoJSON but caused QGIS to hang):
  - <base>_points.geojson     "listing_point" features
  - <base>_polygons.geojson   "target_parcel" + "neighbor_parcel" features
  - <base>_lines.geojson      "listing_to_neighbor_line" QC features
All share a "feature_type" property (for styling) and "listing_id" (so a
GIS tool can group/filter everything belonging to one listing across files).
Neighbor/target features also carry "land_use_desc" (Single Family / Land /
Condo - No Land / Common Land / etc. -- None if no VGSI match).

Usage:
    python find_abutters.py lincoln_nh.geojson lincoln_joined.geojson lincoln_sfh_land.json lincoln_abutters [close_radius_m=100] [far_radius_m=250] [lot_size_ratio_tolerance=2.5]
    (writes lincoln_abutters_points.geojson, lincoln_abutters_polygons.geojson, lincoln_abutters_lines.geojson)
"""

import sys
import json
import csv
import re
import math
from shapely.geometry import shape, Point
from shapely.ops import transform

_SUFFIX_MAP = {
    "ROAD": "RD", "STREET": "ST", "LANE": "LN", "DRIVE": "DR",
    "AVENUE": "AVE", "MOUNTAIN": "MTN", "TRAIL": "TRL", "CIRCLE": "CIR",
    "COURT": "CT", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "PLACE": "PL",
}

ALLOWED_PROPERTY_TYPES = {"Single Family", "Land"}
NEIGHBOR_BUFFER_DEG = 0.00005  # ~5m tolerance for boundary slivers/gaps
SQM_PER_ACRE = 4046.8564224

# Which VGSI land_use_desc values count as a valid CANDIDATE worth showing
# (find_abutters.py's job -- is this neighbor comparable enough to include
# at all). Informational tag only here (see comp_eligible below) -- not
# used to exclude anything, since a wrong guess here would silently hide
# real candidates. Extend this set if more towns' data surfaces other
# land-use labels that should qualify.
COMP_ELIGIBLE_LAND_USE = {"Single Family", "Vacant Land", "Vacant - Pot Dev"}

# Which VGSI land_use_desc values count as a real VALUE COMP -- i.e. belong
# in a median calculation (compute_gap.py's job). Narrower than the set
# above on purpose: land and finished homes are fundamentally different
# value classes, so pooling a $165K vacant lot with $1.5M built homes into
# one median doesn't make sense even though both are legitimate neighbors
# worth showing. A target's value -- whether it's a house being priced, or
# land being evaluated for development upside -- should be measured
# against real built-home values, never against land prices.
VALUE_COMP_LAND_USE = {"Single Family"}


def polygon_area_acres(geom) -> float:
    """
    Approximate a WGS84 (lon/lat degrees) polygon's area in acres using a
    local flat-earth projection -- good enough at parcel scale (a lot spans
    a tiny fraction of a degree, so curvature error is negligible). Avoids
    pulling in a full reprojection library (pyproj) just for this.
    Handles MultiPolygon geometries (some parcels have multiple disjoint
    pieces, e.g. a lot split by a right-of-way) by summing area across parts.
    """
    poly = shape(geom)
    parts = list(poly.geoms) if poly.geom_type == "MultiPolygon" else [poly]

    total_area_m2 = 0.0
    for part in parts:
        lon_lats = list(part.exterior.coords)
        avg_lat_rad = math.radians(sum(lat for lon, lat in lon_lats) / len(lon_lats))
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * math.cos(avg_lat_rad)
        projected = [(lon * m_per_deg_lon, lat * m_per_deg_lat) for lon, lat in lon_lats]

        area_m2 = 0.0
        for i in range(len(projected) - 1):
            x1, y1 = projected[i]
            x2, y2 = projected[i + 1]
            area_m2 += x1 * y2 - x2 * y1
        total_area_m2 += abs(area_m2) / 2.0

    return total_area_m2 / SQM_PER_ACRE


def normalize_address(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"[^\w\s]", " ", raw.upper())
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [_SUFFIX_MAP.get(tok, tok) for tok in s.split(" ")]
    return " ".join(tokens)


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


def build_address_index(raw_features: list[dict]) -> dict[str, dict]:
    by_address = {}
    collisions = set()
    for feat in raw_features:
        key = normalize_address(feat["properties"].get("StreetAddress", ""))
        if not key:
            continue
        if key in by_address and key not in collisions:
            collisions.add(key)
        by_address[key] = feat
    for key in collisions:
        del by_address[key]
    return by_address


def resolve_listing_to_parcel(listing: dict, address_index: dict, raw_features: list[dict]) -> tuple[dict | None, str]:
    """
    Returns (parcel_feature_or_None, method) where method is 'point_in_polygon',
    'address', 'unresolved', or 'unresolved_suspect_parcel'. Point-in-polygon
    is tried first -- the listing's own lat/lon (supplied directly by
    RentCast, no separate geocoding needed) is ground truth, more reliable
    than string-matching address formatting quirks. Address matching is the
    fallback, for the rare case a point falls just outside every polygon.

    If the point falls inside MULTIPLE parcels, prefer the smallest one --
    non-overlapping parcels shouldn't both truly contain the same point, so
    multiple matches signals messy/overlapping source geometry.

    Sanity check: if the resolved parcel's acreage is wildly larger than the
    listing's own reported lotSize, reject it and fall back to address
    matching instead. This came up for real: a brand-new subdivision lot's
    coordinate landed inside a ~64,000-acre parcel (the underlying land
    tract's old boundary, labeled "KANCAMAGUS HIGHWAY") because GRANIT
    hadn't yet been updated to reflect the actual small platted lot -- the
    real lot polygon doesn't exist yet to match against. No resolution
    logic can find a polygon that isn't there; the right move is to detect
    and reject the bad match rather than silently return it.
    """
    lat, lon = listing.get("latitude"), listing.get("longitude")
    lot_size_sqft = listing.get("lotSize")
    expected_acres = (lot_size_sqft / 43560) if lot_size_sqft else None

    def is_plausible_size(candidate_acres: float) -> bool:
        if expected_acres is None:
            # No stated lot size to check against -- fall back to an
            # absolute cap. Even a large rural NH lot is rarely >200 acres;
            # this only exists to catch town-spanning corridor parcels.
            return candidate_acres <= 200
        return candidate_acres <= max(expected_acres * 20, 5)

    if lat is not None and lon is not None:
        pt = Point(lon, lat)
        candidates = [feat for feat in raw_features if shape(feat["geometry"]).buffer(NEIGHBOR_BUFFER_DEG).contains(pt)]
        if candidates:
            smallest = min(candidates, key=lambda f: polygon_area_acres(f["geometry"]))
            smallest_acres = polygon_area_acres(smallest["geometry"])
            if is_plausible_size(smallest_acres):
                return smallest, "point_in_polygon"
            # Suspect match -- don't use it, but remember we had one so the
            # caller can distinguish "no polygon contained the point" from
            # "a polygon did, but it looked wrong" if address matching also fails.
            suspect = True
        else:
            suspect = False
    else:
        suspect = False

    addr_key = normalize_address(listing.get("addressLine1", ""))
    parcel = address_index.get(addr_key)
    if parcel is not None:
        return parcel, "address"

    return None, "unresolved_suspect_parcel" if suspect else "unresolved"


def project_to_local_meters(geom, ref_lat: float):
    """
    Reproject a shapely geometry from WGS84 (lon/lat degrees) to a local
    flat-earth meters coordinate system, using ref_lat to scale longitude
    correctly (a degree of longitude is shorter than a degree of latitude
    away from the equator -- at 44N, by a factor of ~cos(44) = 0.72). Good
    enough at neighborhood scale (hundreds of meters); not meant for
    anything requiring true geodesic accuracy.
    """
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(ref_lat))
    return transform(lambda x, y, z=None: (x * m_per_deg_lon, y * m_per_deg_lat), geom)


def distance_m(geom_a, geom_b, ref_lat: float) -> float:
    """Boundary-to-boundary distance in meters between two WGS84 geometries."""
    a = project_to_local_meters(geom_a, ref_lat)
    b = project_to_local_meters(geom_b, ref_lat)
    return a.distance(b)


def street_name_only(street_address: str) -> str:
    """Normalized street name with any leading house number stripped, so
    '184 CROOKED MTN ROAD' and 'CROOKED MOUNTAIN ROAD #101' both reduce to
    'CROOKED MTN RD' (the existing suffix normalization already collapses
    MOUNTAIN/MTN and ROAD/RD, so this is an exact match on the normalized
    form -- not fuzzy string matching, but it covers the abbreviation
    variants we've actually seen in this data)."""
    normalized = normalize_address(street_address)
    tokens = normalized.split(" ")
    if tokens and tokens[0].isdigit():
        return " ".join(tokens[1:])
    return normalized


def lot_size_similar(acres_a: float, acres_b: float, ratio_tolerance: float = 2.5) -> bool:
    """True if the larger of the two lot sizes is no more than
    ratio_tolerance times the smaller -- e.g. a 0.5-acre lot and a
    1.0-acre lot pass at the default 2.5x tolerance; a 0.3-acre lot next
    to a 60-acre Current Use tract does not."""
    if acres_a <= 0 or acres_b <= 0:
        return False
    lo, hi = sorted([acres_a, acres_b])
    return (hi / lo) <= ratio_tolerance


def find_nearby_by_rules(
    raw_features: list[dict],
    target: dict,
    close_radius_m: float = 100,
    far_radius_m: float = 250,
    lot_size_ratio_tolerance: float = 2.5,
) -> dict[str, tuple[dict, str]]:
    """
    Return candidate neighbors (not counting true geometric touching, which
    find_neighbors already handles) using two rules:
      - within close_radius_m AND similar lot size to the target -> "near_similar_size"
      - within far_radius_m AND same normalized street name as the target -> "near_same_street"
    Returns {pid: (feature, reason)}. A parcel matching both rules keeps
    whichever reason is checked first (near_similar_size).
    """
    target_geom = shape(target["geometry"])
    target_centroid = target_geom.centroid
    ref_lat = target_centroid.y
    target_acres = polygon_area_acres(target["geometry"])
    target_street = street_name_only(target["properties"].get("StreetAddress", ""))
    target_pid = str(target["properties"].get("PID", "")).strip()

    results: dict[str, tuple[dict, str]] = {}
    for feat in raw_features:
        pid = str(feat["properties"].get("PID", "")).strip()
        if pid == target_pid:
            continue

        d = distance_m(target_geom, shape(feat["geometry"]), ref_lat)
        if d > far_radius_m:
            continue

        if d <= close_radius_m:
            other_acres = polygon_area_acres(feat["geometry"])
            if lot_size_similar(target_acres, other_acres, lot_size_ratio_tolerance):
                results[pid] = (feat, "near_similar_size")
                continue

        other_street = street_name_only(feat["properties"].get("StreetAddress", ""))
        if other_street and other_street == target_street:
            results[pid] = (feat, "near_same_street")

    return results


def find_neighbors(raw_features: list[dict], target: dict, buffer_deg: float = NEIGHBOR_BUFFER_DEG) -> list[dict]:
    """
    Return every parcel (other than the target) whose polygon touches or
    nearly touches the target's polygon. buffer_deg is a small tolerance
    (~5 meters at this latitude) to catch adjacent parcels that don't share
    an exact boundary line due to survey/digitizing differences.
    """
    target_poly = shape(target["geometry"]).buffer(NEIGHBOR_BUFFER_DEG)
    target_pid = target["properties"].get("PID")
    neighbors = []
    for feat in raw_features:
        if feat["properties"].get("PID") == target_pid:
            continue
        if target_poly.intersects(shape(feat["geometry"])):
            neighbors.append(feat)
    return neighbors


def main():
    if len(sys.argv) < 5:
        print("Usage: python find_abutters.py <raw_parcels.geojson> <joined_parcels.geojson> "
              "<listings.json> <output_base> [close_radius_m=100] [far_radius_m=250] [lot_size_ratio_tolerance=2.5]")
        sys.exit(1)

    raw_path, joined_path, listings_path, out_path = sys.argv[1:5]
    close_radius_m = float(sys.argv[5]) if len(sys.argv) > 5 else 100
    far_radius_m = float(sys.argv[6]) if len(sys.argv) > 6 else 250
    lot_size_ratio_tolerance = float(sys.argv[7]) if len(sys.argv) > 7 else 2.5

    raw_features = load_json(raw_path)["features"]
    listings = load_json(listings_path)
    joined_features = load_json(joined_path)["features"]
    land_use_by_pid = {
        str(f["properties"].get("PID", "")).strip(): f["properties"].get("land_use_desc")
        for f in joined_features
    }
    value_by_pid = {
        str(f["properties"].get("PID", "")).strip(): f["properties"].get("total_market_value")
        for f in joined_features
    }

    address_index = build_address_index(raw_features)

    output_features = []
    unresolved_listings = []
    listing_count = 0
    neighbor_row_count = 0
    total_invalid_excluded = 0
    total_size_mismatch_excluded = 0
    total_type_mismatch_excluded = 0
    neighbors_missing_land_use = 0

    for listing in listings:
        if listing.get("propertyType") not in ALLOWED_PROPERTY_TYPES:
            continue

        parcel, method = resolve_listing_to_parcel(listing, address_index, raw_features)
        if parcel is None:
            unresolved_listings.append(listing)
            continue

        listing_count += 1
        listing_id = listing.get("id") or listing.get("addressLine1")

        # 1) The listing's own point, straight from RentCast's lat/lon
        output_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [listing.get("longitude"), listing.get("latitude")],
            },
            "properties": {
                "feature_type": "listing_point",
                "listing_id": listing_id,
                "listing_address": listing.get("addressLine1"),
                "listing_type": listing.get("propertyType"),
                "listing_price": listing.get("price"),
                "listing_resolve_method": method,
                "listing_mls_number": listing.get("mlsNumber"),
            },
        })

        # 2) The target parcel's own polygon (useful to see the subject
        # property's actual boundary, not just a dot)
        target_pid = str(parcel["properties"].get("PID", "")).strip()
        target_acres = polygon_area_acres(parcel["geometry"])
        output_features.append({
            "type": "Feature",
            "geometry": parcel["geometry"],
            "properties": {
                "feature_type": "target_parcel",
                "listing_id": listing_id,
                "listing_address": listing.get("addressLine1"),
                "pid": target_pid,
                "address": parcel["properties"].get("StreetAddress"),
                "sluc": parcel["properties"].get("SLU"),
                "land_use_desc": land_use_by_pid.get(target_pid),
                "total_market_value": value_by_pid.get(target_pid),
                "lot_acres": round(target_acres, 2),
            },
        })

        # 3) Neighbor polygons: true geometric touching (always included) +
        # distance-based candidates (close + similar size, or farther +
        # same street name)
        geometric = find_neighbors(raw_features, parcel)
        nearby = find_nearby_by_rules(raw_features, parcel, close_radius_m, far_radius_m, lot_size_ratio_tolerance)

        by_pid: dict[str, dict] = {}
        neighbor_methods: dict[str, set] = {}
        for n in geometric:
            pid = str(n["properties"].get("PID", "")).strip()
            by_pid[pid] = n
            neighbor_methods.setdefault(pid, set()).add("geometric")
        for pid, (n, reason) in nearby.items():
            by_pid[pid] = n
            neighbor_methods.setdefault(pid, set()).add(reason)

        invalid_excluded = 0
        for pid, n in by_pid.items():
            addr = n["properties"].get("StreetAddress")
            # Only exclude genuinely blank addresses -- nothing to identify
            # the parcel by at all. We used to also exclude anything with a
            # '#' (assuming it meant a condo unit), but that's not reliable:
            # some parcels use '#NNN' for a numbered land lot, not a unit
            # (e.g. 'CROOKED MTN ROAD #101' is Vacant Land, not a condo) --
            # address punctuation alone can't tell those apart. land_use_desc
            # is the real signal now; address shape is kept only as an
            # informational tag below, never as an exclusion rule.
            if not (addr or "").strip():
                invalid_excluded += 1
                continue

            # Candidates, not just "nearby things" -- discard anything that
            # fails size or (known) type comparability here, rather than
            # deferring to compute_gap.py. find_abutters.py's job is to
            # produce plausible comps, not everything within reach.
            #
            # Size check is skipped for Land targets: a land listing's own
            # acreage isn't really what matters for comparability -- the
            # question is what the neighborhood supports (nearby built
            # homes), not whether the raw lot size matches. Requiring
            # symmetry wrongly excluded genuinely close comps (e.g. a
            # 1.11-acre land listing next to 0.35-acre built homes on the
            # same small circle -- clearly comparable by proximity, but
            # failed a strict size ratio). Still applies for Single Family
            # targets, where house-to-house size comparability matters.
            target_is_land = listing.get("propertyType") == "Land"
            neighbor_acres = polygon_area_acres(n["geometry"])
            if not target_is_land and not lot_size_similar(target_acres, neighbor_acres, lot_size_ratio_tolerance):
                total_size_mismatch_excluded += 1
                continue

            land_use_desc = land_use_by_pid.get(pid)
            # Discard a KNOWN non-comparable type (Commercial, Condo, Common
            # Land, etc.). Keep unknown type (no VGSI match, land_use_desc
            # is None) rather than guessing it's bad -- we already learned
            # the hard way (the 'CROOKED MTN ROAD #101' case) that assuming
            # "probably not comparable" from incomplete data silently hides
            # real candidates. Unknown stays in, tagged as unverified.
            if land_use_desc is not None and land_use_desc not in COMP_ELIGIBLE_LAND_USE:
                total_type_mismatch_excluded += 1
                continue
            if land_use_desc is None:
                neighbors_missing_land_use += 1

            output_features.append({
                "type": "Feature",
                "geometry": n["geometry"],
                "properties": {
                    "feature_type": "neighbor_parcel",
                    "listing_id": listing_id,
                    "listing_address": listing.get("addressLine1"),
                    "pid": pid,
                    "address": addr,
                    "sluc": n["properties"].get("SLU"),
                    "land_use_desc": land_use_desc,
                    # True (known comparable type) or None (unverified --
                    # no VGSI match, kept because size already checked out
                    # and we can't confirm it's actually bad). Never False
                    # here anymore -- a known-bad type is discarded above,
                    # not just tagged.
                    "comp_eligible": True if land_use_desc in COMP_ELIGIBLE_LAND_USE else None,
                    "has_unit_style_address": "#" in addr,
                    "total_market_value": value_by_pid.get(pid),
                    "lot_acres": round(neighbor_acres, 2),
                    "found_via": "+".join(sorted(neighbor_methods[pid])),
                },
            })
            neighbor_row_count += 1

            # QC linestring: listing's own coordinate -> this neighbor's
            # centroid. We'd switched this to nearest-boundary-point earlier
            # to fix multi-km lines to huge parcels (a highway corridor, a
            # 142-acre common-land parcel) -- but the real cause of those
            # was bad target resolution and un-filtered outlier neighbors,
            # both fixed upstream since (the lotSize sanity check on
            # resolve_listing_to_parcel, and the size/type discard rules
            # above). With those gone, every surviving candidate is already
            # comparable in size to the target, so its centroid is a
            # reasonably close, visually cleaner point than a boundary edge.
            neighbor_centroid = shape(n["geometry"]).centroid
            output_features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [listing.get("longitude"), listing.get("latitude")],
                        [neighbor_centroid.x, neighbor_centroid.y],
                    ],
                },
                "properties": {
                    "feature_type": "listing_to_neighbor_line",
                    "listing_id": listing_id,
                    "listing_address": listing.get("addressLine1"),
                    "neighbor_pid": pid,
                    "neighbor_address": addr,
                    "found_via": "+".join(sorted(neighbor_methods[pid])),
                    "land_use_desc": land_use_desc,
                },
            })
        total_invalid_excluded += invalid_excluded

    # Split by geometry type before writing -- a single FeatureCollection
    # mixing Point/Polygon/LineString geometries is valid GeoJSON, but it
    # caused QGIS to hang trying to auto-detect sublayers. Writing one
    # file per geometry type loads cleanly with no ambiguity, and lets you
    # style points/polygons/lines independently anyway.
    out_base = out_path[:-len(".geojson")] if out_path.endswith(".geojson") else out_path
    points = [f for f in output_features if f["geometry"]["type"] == "Point"]
    polygons = [f for f in output_features if f["geometry"]["type"] in ("Polygon", "MultiPolygon")]
    lines = [f for f in output_features if f["geometry"]["type"] == "LineString"]

    # Even within the "polygon family," mixing literal Polygon and
    # MultiPolygon types can still trip QGIS's geometry-homogeneity check.
    # Normalize every feature to MultiPolygon (wrapping a plain Polygon's
    # single ring-set as a one-part MultiPolygon) so the file is uniformly
    # one geometry type with no ambiguity left for QGIS to resolve.
    for feat in polygons:
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            geom["type"] = "MultiPolygon"
            geom["coordinates"] = [geom["coordinates"]]

    split_outputs = {
        f"{out_base}_points.geojson": points,
        f"{out_base}_polygons.geojson": polygons,
        f"{out_base}_lines.geojson": lines,
    }
    for path, features in split_outputs.items():
        with open(path, "w") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f)

    # Combined file, in addition to the split ones above -- kept for
    # convenience (e.g. tools other than QGIS, or a quick single-file
    # share), with the same MultiPolygon normalization applied. Note this
    # still mixes Point/MultiPolygon/LineString geometry families, which is
    # what caused QGIS to hang in the first place -- the split files above
    # are the reliable option if this one causes trouble again.
    combined_path = f"{out_base}_combined.geojson"
    with open(combined_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": points + polygons + lines}, f)

    eligible = [l for l in listings if l.get("propertyType") in ALLOWED_PROPERTY_TYPES]
    print(f"Eligible listings (Single Family + Land): {len(eligible)}")
    print(f"Resolved to a parcel: {listing_count}")
    print(f"Unresolved: {len(unresolved_listings)}")
    if unresolved_listings:
        for l in unresolved_listings:
            print(f"  {l.get('addressLine1')} ({l.get('propertyType')})")
    print(f"Neighbor rules: geometric touching (always) + within {close_radius_m:.0f}m with similar lot size "
          f"(<= {lot_size_ratio_tolerance}x ratio) + within {far_radius_m:.0f}m on the same street")
    print(f"Neighbors excluded (blank address -- nothing to identify by): {total_invalid_excluded}")
    print(f"Neighbors excluded (lot size not comparable): {total_size_mismatch_excluded}")
    print(f"Neighbors excluded (known non-comparable type): {total_type_mismatch_excluded}")
    print(f"Neighbors with no VGSI land_use_desc match: {neighbors_missing_land_use}")
    print(f"Features written: {len(output_features)} total "
          f"({listing_count} listing points + {listing_count} target parcels + "
          f"{neighbor_row_count} neighbor parcels + {neighbor_row_count} QC linestrings)")
    for path, features in split_outputs.items():
        print(f"  {path}: {len(features)} features")
    print(f"  {combined_path}: {len(points) + len(polygons) + len(lines)} features "
          f"(combined, all geometry types -- may hang QGIS like the original did; "
          f"use the split files above if so)")
    print()
    print("Note: land_use_desc (property type) and total_market_value (assessed "
          "value) are both included for display/QC -- this script still doesn't "
          "compute anything with them (no gap, no ranking); that's compute_gap.py. "
          "'feature_type' property distinguishes listing_point / target_parcel / "
          "neighbor_parcel for map styling.")


if __name__ == "__main__":
    main()