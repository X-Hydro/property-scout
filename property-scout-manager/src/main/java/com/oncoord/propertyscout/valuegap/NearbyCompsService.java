package com.oncoord.propertyscout.valuegap;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.propertyscout.geo.ParcelGeometryUtils;
import com.oncoord.propertyscout.model.PropertyType;
import org.locationtech.jts.geom.MultiPolygon;
import org.locationtech.jts.geom.Polygon;
import org.locationtech.jts.io.WKTReader;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Point;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.EnumSet;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * Part 1 of the ValueGap pipeline: given a target listing's coordinates,
 * find its own parcel and the candidate neighbor parcels worth treating as
 * comps. Direct port of find_abutters.py's rules onto PostGIS, using the
 * real parcel polygons already stored in property_values.geometry rather
 * than re-deriving anything from raw GRANIT files.
 *
 * Known simplification vs. the Python version: target-parcel resolution
 * here is point-in-polygon plus the implausible-size sanity check, but
 * NOT the address-matching fallback. Python's resolve_listing_to_parcel()
 * falls back to address matching when point-in-polygon finds nothing, or
 * when it finds something but rejects it as implausibly large. Here, both
 * of those cases just return Optional.empty() -- same as Python's
 * "unresolved" case, just without the second attempt.
 */
@Service
public class NearbyCompsService {

    private static final double DEFAULT_CLOSE_RADIUS_M = 100;
    private static final double DEFAULT_FAR_RADIUS_M = 250;
    private static final double DEFAULT_LOT_SIZE_RATIO_TOLERANCE = 2.5;
    private static final double TOUCH_TOLERANCE_M = 5; // ~ NEIGHBOR_BUFFER_DEG in find_abutters.py
    private static final double SQM_PER_ACRE = 4046.8564224; // matches find_abutters.py's constant exactly
    private static final double SQFT_PER_ACRE = 43560;
    private static final double ABSOLUTE_MAX_PLAUSIBLE_ACRES = 200; // no stated lot size to check against

    private final ObjectMapper objectMapper;


    // Which PropertyType values (== VGSI land_use_desc) are worth showing as
    // a candidate at all. Anything else -- a raw string that doesn't map to
    // any PropertyType (Commercial, Condo - No Land, Common Land, etc.) --
    // is discarded; a genuinely missing value (raw null, no assessor match)
    // is kept and tagged unverified rather than guessed away. Those are two
    // different cases even though PropertyType.fromValue() returns null for
    // both -- see the null-check-on-the-raw-string below. See
    // find_abutters.py's COMP_ELIGIBLE_LAND_USE for the original reasoning.
    private static final Set<PropertyType> COMP_ELIGIBLE_TYPES =
            EnumSet.of(PropertyType.SINGLE_FAMILY, PropertyType.VACANT_LAND);

    private final JdbcTemplate jdbcTemplate;

    public NearbyCompsService(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper ) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;

    }

    /**
     * Resolve a listing's lat/lon to its own parcel via point-in-polygon,
     * with the same sanity check as Python's is_plausible_size(): a
     * resolved parcel whose acreage is wildly larger than the listing's
     * own reported lot size is rejected rather than trusted (the "brand-new
     * subdivision lot's point lands inside the old, not-yet-subdivided
     * parent tract" case). Acreage for this check is computed fresh from
     * the polygon itself (ST_Area), not read from the acreage attribute
     * column, which can be null or stale -- same as Python recomputing
     * polygon_area_acres every time rather than trusting a stored value.
     *
     * If the point falls inside more than one polygon, prefers the
     * smallest (mirrors Python's "overlapping geometry -> take the
     * smallest" rule) -- and it's that smallest one's size which gets
     * sanity-checked, matching Python exactly.
     *
     * @param listingLotSizeSqFt the listing's own reported lot size in
     *                            square feet (RentCast's units), or null if
     *                            not reported. Null falls back to an
     *                            absolute 200-acre cap, same as Python.
     */
    public Optional<TargetParcel> resolveTargetParcel(
            double latitude, double longitude, Double listingLotSizeSqFt) {

        String sql = """
        SELECT property_id, ST_AsText(geometry) AS geom_wkt,
               ST_AsGeoJSON(geometry) AS geom_geojson,
               address, acreage,
               assessed_value, property_type,
               ST_Area(geometry::geography) / ? AS computed_acres
        FROM property_values
        WHERE geometry IS NOT NULL
          AND ST_Contains(geometry, ST_SetSRID(ST_MakePoint(?, ?), 4326))
        ORDER BY ST_Area(geometry::geography) ASC
        LIMIT 1
        """;

        Double expectedAcres = listingLotSizeSqFt == null ? null : listingLotSizeSqFt / SQFT_PER_ACRE;

        return jdbcTemplate.query(sql, rs -> {
            if (!rs.next()) {
                return Optional.empty();
            }
            double computedAcres = rs.getDouble("computed_acres");
            if (!isPlausibleSize(computedAcres, expectedAcres)) {
                return Optional.empty();
            }
            Double storedAcres = (Double) rs.getObject("acreage");
            Double effectiveAcres = storedAcres != null ? storedAcres : computedAcres;
            return Optional.of(new TargetParcel(
                    rs.getString("property_id"),
                    rs.getString("geom_wkt"),
                    rs.getString("geom_geojson"),
                    rs.getString("address"),
                    effectiveAcres,
                    (Double) rs.getObject("assessed_value"),
                    PropertyType.fromValue(rs.getString("property_type"))
            ));
        }, SQM_PER_ACRE, longitude, latitude);
    }

    /** Direct port of find_abutters.py's is_plausible_size(). */
    private boolean isPlausibleSize(double candidateAcres, Double expectedAcres) {
        if (expectedAcres == null) {
            return candidateAcres <= ABSOLUTE_MAX_PLAUSIBLE_ACRES;
        }
        return candidateAcres <= Math.max(expectedAcres * 20, 5);
    }

    public List<CompCandidate> findComps(TargetParcel target, boolean targetIsLand) {
        return findComps(target, targetIsLand, DEFAULT_CLOSE_RADIUS_M, DEFAULT_FAR_RADIUS_M, DEFAULT_LOT_SIZE_RATIO_TOLERANCE);
    }

    public List<CompCandidate> findComps(
            TargetParcel target,
            boolean targetIsLand,
            double closeRadiusM,
            double farRadiusM,
            double lotSizeRatioTolerance) {

        String targetStreet = ValueGapUtils.streetNameOnly(target.getAddress());

        String sql = """
        SELECT property_id, address, city, property_type, assessed_value, acreage,
               latitude, longitude,
               ST_AsGeoJSON(geometry) AS geom_geojson,
               ST_AsText(geometry) AS geom_wkt,
               ST_Distance(geometry::geography, ST_GeomFromText(?, 4326)::geography) AS distance_m,
               ST_DWithin(geometry::geography, ST_GeomFromText(?, 4326)::geography, ?) AS touches
        FROM property_values
        WHERE geometry IS NOT NULL
          AND property_id <> ?
          AND ST_DWithin(geometry::geography, ST_GeomFromText(?, 4326)::geography, ?)
        """;

        List<CompCandidate> candidates = new ArrayList<>();
        String wkt = target.getGeometryWkt();

        WKTReader wktReader = new WKTReader();
        Geometry targetGeom;
        try {
            targetGeom = wktReader.read(wkt);
        } catch (Exception e) {
            return candidates; // target's own WKT should always parse
        }
        Point targetCentroid = targetGeom.getCentroid();
        double targetLat = targetCentroid.getY();
        double targetLon = targetCentroid.getX();

        jdbcTemplate.query(sql, rs -> {
            double distanceMeters = rs.getDouble("distance_m");
            boolean touches = rs.getBoolean("touches");
            Double acres = (Double) rs.getObject("acreage");
            String address = rs.getString("address");
            String rawPropertyType = rs.getString("property_type");
            PropertyType propertyType = PropertyType.fromValue(rawPropertyType);
            String propertyId = rs.getString("property_id");

            String candidateWkt = rs.getString("geom_wkt");
            Geometry candidateGeom = null;
            if (candidateWkt != null) {
                try {
                    candidateGeom = wktReader.read(candidateWkt);
                } catch (Exception e) {
                    // leave null -- quality checks below are skipped for this row
                }
            }

            JsonNode geometry = null;
            String geomGeoJson = rs.getString("geom_geojson");
            if (geomGeoJson != null) {
                try {
                    geometry = objectMapper.readTree(geomGeoJson);
                } catch (Exception e) {
                    // leave null
                }
            }
            if (candidateGeom != null) {
                if (!(candidateGeom instanceof Polygon) && !(candidateGeom instanceof MultiPolygon)) {
                    return; // not a real parcel shape -- a bare Point or similar
                }
                if (ParcelGeometryUtils.countMultiPoly(candidateGeom) > 2) {
                    return;
                }
                if (ParcelGeometryUtils.countHoles(candidateGeom) > 2) {
                    return;
                }
                if (ParcelGeometryUtils.furthestPointDistanceM(candidateGeom, targetLat, targetLon) > 500.0) {
                    return;
                }
            }

            Set<String> foundVia = new HashSet<>();
            if (touches) {
                foundVia.add("geometric");
            }
            if (distanceMeters <= closeRadiusM
                    && ValueGapUtils.lotSizeSimilar(target.getAcres(), acres, lotSizeRatioTolerance)) {
                foundVia.add("near_similar_size");
            }
            if (distanceMeters <= farRadiusM
                    && !targetStreet.isEmpty()
                    && targetStreet.equals(ValueGapUtils.streetNameOnly(address))) {
                foundVia.add("near_same_street");
            }
            if (foundVia.isEmpty()) {
                return;
            }

            if (address == null || address.isBlank()) {
                return;
            }
            if (!ValueGapUtils.lotSizeSimilar(target.getAcres(), acres, lotSizeRatioTolerance)) {
                return;
            }
            if (rawPropertyType != null && !COMP_ELIGIBLE_TYPES.contains(propertyType)) {
                return;
            }
            Boolean compEligible = rawPropertyType == null ? null : COMP_ELIGIBLE_TYPES.contains(propertyType);

            candidates.add(new CompCandidate(
                    propertyId,
                    address,
                    rs.getString("city"),
                    propertyType,
                    (Double) rs.getObject("assessed_value"),
                    acres,
                    distanceMeters,
                    rs.getDouble("latitude"),
                    rs.getDouble("longitude"),
                    foundVia,
                    compEligible,
                    geometry
            ));
        }, wkt, wkt, TOUCH_TOLERANCE_M, target.getPropertyId(), wkt, farRadiusM);

        return candidates;
    }
}