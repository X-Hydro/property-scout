package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.PropertyType;
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
 * here is point-in-polygon only. Python's resolve_listing_to_parcel() also
 * falls back to address matching when no polygon contains the point, and
 * rejects a resolved parcel if it's implausibly large vs. the listing's own
 * reported lot size (the "subdivision lot not yet in GRANIT" case). Neither
 * fallback is implemented yet -- an unresolved target here just returns
 * Optional.empty() from resolveTargetParcel, same as Python's "unresolved"
 * case, just without the second attempt.
 */
@Service
public class NearbyCompsService {

    private static final double DEFAULT_CLOSE_RADIUS_M = 100;
    private static final double DEFAULT_FAR_RADIUS_M = 250;
    private static final double DEFAULT_LOT_SIZE_RATIO_TOLERANCE = 2.5;
    private static final double TOUCH_TOLERANCE_M = 5; // ~ NEIGHBOR_BUFFER_DEG in find_abutters.py

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

    public NearbyCompsService(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Resolve a listing's lat/lon to its own parcel via point-in-polygon.
     * If the point falls inside more than one polygon, prefers the smallest
     * (mirrors Python's "overlapping geometry -> take the smallest" rule).
     */
    public Optional<TargetParcel> resolveTargetParcel(double latitude, double longitude) {
        String sql = """
            SELECT property_id, ST_AsText(geometry) AS geom_wkt, address, acreage,
                   assessed_value, property_type
            FROM property_values
            WHERE geometry IS NOT NULL
              AND ST_Contains(geometry, ST_SetSRID(ST_MakePoint(?, ?), 4326))
            ORDER BY ST_Area(geometry::geography) ASC
            LIMIT 1
            """;

        return jdbcTemplate.query(sql, rs -> {
            if (!rs.next()) {
                return Optional.empty();
            }
            return Optional.of(new TargetParcel(
                    rs.getString("property_id"),
                    rs.getString("geom_wkt"),
                    rs.getString("address"),
                    (Double) rs.getObject("acreage"),
                    (Double) rs.getObject("assessed_value"),
                    PropertyType.fromValue(rs.getString("property_type"))
            ));
        }, longitude, latitude);
    }

    public List<CompCandidate> findComps(TargetParcel target, boolean targetIsLand) {
        return findComps(target, targetIsLand, DEFAULT_CLOSE_RADIUS_M, DEFAULT_FAR_RADIUS_M, DEFAULT_LOT_SIZE_RATIO_TOLERANCE);
    }

    /**
     * Stage A (gather) + Stage B (filter), same two-pass structure as
     * find_abutters.py:
     *   A. a candidate is anything that's geometrically touching (within
     *      TOUCH_TOLERANCE_M), OR within closeRadiusM with a similar lot
     *      size, OR within farRadiusM on the same normalized street name.
     *   B. every candidate from A is then filtered uniformly: blank address
     *      excluded; lot size must be comparable to the target UNLESS the
     *      target itself is Land (a land listing's own acreage isn't what
     *      matters -- what matters is what the neighborhood supports);
     *      a known non-comparable type (present but not in
     *      COMP_ELIGIBLE_TYPES) is excluded, but a genuinely missing type
     *      (raw column is null) is kept and tagged unverified rather than
     *      guessed away.
     */
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
                   ST_Distance(geometry::geography, ST_GeomFromText(?, 4326)::geography) AS distance_m,
                   ST_DWithin(geometry::geography, ST_GeomFromText(?, 4326)::geography, ?) AS touches
            FROM property_values
            WHERE geometry IS NOT NULL
              AND property_id <> ?
              AND ST_DWithin(geometry::geography, ST_GeomFromText(?, 4326)::geography, ?)
            """;

        List<CompCandidate> candidates = new ArrayList<>();
        String wkt = target.getGeometryWkt();

        jdbcTemplate.query(sql, rs -> {
            double distanceMeters = rs.getDouble("distance_m");
            boolean touches = rs.getBoolean("touches");
            Double acres = (Double) rs.getObject("acreage");
            String address = rs.getString("address");
            String rawPropertyType = rs.getString("property_type");
            PropertyType propertyType = PropertyType.fromValue(rawPropertyType);

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
                return; // didn't qualify under any rule
            }

            // --- Stage B filtering, same as find_abutters.py ---
            if (address == null || address.isBlank()) {
                return;
            }
            if (!targetIsLand && !ValueGapUtils.lotSizeSimilar(target.getAcres(), acres, lotSizeRatioTolerance)) {
                return;
            }
            // rawPropertyType != null but propertyType == null means "known,
            // just not one of our PropertyType values" (e.g. Commercial) --
            // still excluded, same as before. rawPropertyType == null means
            // genuinely no assessor match -- kept, tagged unverified below.
            if (rawPropertyType != null && !COMP_ELIGIBLE_TYPES.contains(propertyType)) {
                return;
            }
            Boolean compEligible = rawPropertyType == null ? null : COMP_ELIGIBLE_TYPES.contains(propertyType);

            candidates.add(new CompCandidate(
                    rs.getString("property_id"),
                    address,
                    rs.getString("city"),
                    propertyType,
                    (Double) rs.getObject("assessed_value"),
                    acres,
                    distanceMeters,
                    rs.getDouble("latitude"),
                    rs.getDouble("longitude"),
                    foundVia,
                    compEligible
            ));
        }, wkt, wkt, TOUCH_TOLERANCE_M, target.getPropertyId(), wkt, farRadiusM);

        return candidates;
    }
}