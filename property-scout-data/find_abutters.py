"""
Find abutters for every listing — ValueGap (Phase 1: abutter-finding + identification)

For each active listing (Land or Single Family only -- condos/multi-family
skipped per scope), resolves the listing to its own parcel using its
lat/lon (supplied directly by RentCast -- point-in-polygon against GRANIT's
boundaries), falling back to address matching only if the point doesn't
land inside any parcel. Neighbors come from three rules, combined:
  1. True geometric touching (always included, any distance/size/street)
  2. Within close_radius_m (default 100m) AND similar lot size to the
     target (default: within a 2.5x ratio) -- catches "purely adjacent by
     distance, different street name" cases, e.g. a parcel technically on
     South Peak Road that's actually right next to a Crooked Mtn Rd listing
  3. Within far_radius_m (default 250m) AND same normalized street name --
     catches "same street, further down" cases without relying on
     house-number sequence (which broke down around gaps/condos/unusual
     numbering)
Neighbors are included based on land_use_desc (the assessor's own
plain-English property type -- ground truth), not address punctuation: we
previously excluded anything with a '#' in its address on the theory that
meant a condo unit, but that's unreliable -- some parcels use '#NNN' for a
numbered land lot, not a unit (e.g. a parcel on Crooked Mtn Road, '#101',
turned out to be Vacant Land, not a condo). Only genuinely blank addresses
are excluded; everything else is included and tagged with land_use_desc
and a comp_eligible flag (informational, not a filter) so Phase 2 or
manual review can decide what's actually usable as a comp.

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

# Which VGSI land_use_desc values count as a valid comp: an existing
# single-family home, or a lot capable of holding one. Informational tag
# only (see comp_eligible below) -- not used to exclude anything, since a
# wrong guess here would silently hide real candidates. Extend this set if
# more towns' data surfaces other land-use labels that should qualify.
COMP_ELIGIBLE_LAND_USE = {"Single Family", "Vacant Land", "Vacant - Pot Dev"}


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
    'address', or 'unresolved'. Point-in-polygon is tried first -- the
    listing's own lat/lon (supplied directly by RentCast, no separate
    geocoding needed) is ground truth, more reliable than string-matching
    address formatting quirks. Address matching is the fallback, for the
    rare case a point falls just outside every polygon (small gaps/slivers
    in parcel topology, or an imprecise listing coordinate).
    """
    lat, lon = listing.get("latitude"), listing.get("longitude")
    if lat is not None and lon is not None:
        pt = Point(lon, lat)
        for feat in raw_features:
            poly = shape(feat["geometry"])
            if poly.buffer(NEIGHBOR_BUFFER_DEG).contains(pt):
                return feat, "point_in_polygon"

    addr_key = normalize_address(listing.get("addressLine1", ""))
    parcel = address_index.get(addr_key)
    if parcel is not None:
        return parcel, "address"

    return None, "unresolved"


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
                "lot_acres": round(polygon_area_acres(parcel["geometry"]), 2),
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
            land_use_desc = land_use_by_pid.get(pid)
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
                    # Informational, not a filter -- None means "unknown,
                    # no VGSI match" rather than "not eligible". A parcel we
                    # have no data on shouldn't be silently dropped just
                    # because we can't confirm its type.
                    "comp_eligible": (land_use_desc in COMP_ELIGIBLE_LAND_USE) if land_use_desc else None,
                    "has_unit_style_address": "#" in addr,
                    "total_market_value": value_by_pid.get(pid),
                    "lot_acres": round(polygon_area_acres(n["geometry"]), 2),
                    "found_via": "+".join(sorted(neighbor_methods[pid])),
                },
            })
            neighbor_row_count += 1

            # QC linestring: listing's own coordinate -> this neighbor's
            # centroid. Purely visual -- makes it obvious at a glance in a
            # map viewer whether a neighbor is genuinely nearby or whether
            # the distance rules reached further than expected (a long line
            # to something that doesn't look adjacent is an instant red
            # flag during manual review).
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