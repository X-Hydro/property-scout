package com.oncoord.auctionscout.valuegap;


import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.propertyscout.model.PropertyType;
import com.oncoord.propertyscout.valuegap.CompCandidate;
import com.oncoord.propertyscout.valuegap.NearbyCompsService;
import com.oncoord.propertyscout.valuegap.TargetParcel;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowCallbackHandler;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
        import static org.mockito.ArgumentMatchers.*;
        import static org.mockito.Mockito.*;

/**
 * Exercises NearbyCompsService.findComps without a real database.
 *
 * <p>The SQL in findComps uses PostGIS, so the real-SQLite approach
 * used elsewhere in the project won't work here. Instead, JdbcTemplate
 * is mocked to call the RowCallbackHandler with hand-built ResultSet
 * rows. JTS still parses the WKT polygons in pure Java, so the geometry
 * screens (shape type, multiPoly count, hole count, 500m spread) run
 * for real — the mock only bypasses the database lookup.
 *
 * <p>Requires mockito-core on the test classpath:
 * <pre>
 *   &lt;dependency&gt;
 *     &lt;groupId&gt;org.mockito&lt;/groupId&gt;
 *     &lt;artifactId&gt;mockito-core&lt;/artifactId&gt;
 *     &lt;scope&gt;test&lt;/scope&gt;
 *   &lt;/dependency&gt;
 * </pre>
 */
class NearbyCompsServiceTest {

    // A small square parcel in southern NH (~77 m × 56 m, ≈ 0.1 acres).
    // Centroid ≈ (43.00025°N, -71.5005°W).
    private static final String TARGET_WKT =
            "POLYGON((-71.5010 43.0000, -71.5000 43.0000, " +
                    "-71.5000 43.0005, -71.5010 43.0005, -71.5010 43.0000))";

    // Immediately west of the target — shares one edge, so the furthest
    // vertex is ~125 m from the target centroid (well within the 500 m screen).
    private static final String ADJACENT_WKT =
            "POLYGON((-71.5020 43.0000, -71.5010 43.0000, " +
                    "-71.5010 43.0005, -71.5020 43.0005, -71.5020 43.0000))";

    // ~40 km west — furthestPointDistanceM ≈ 40,000 m, over the 500 m geometry screen.
    private static final String DISTANT_WKT =
            "POLYGON((-72.0000 43.0000, -71.9990 43.0000, " +
                    "-71.9990 43.0005, -72.0000 43.0005, -72.0000 43.0000))";

    private JdbcTemplate jdbcTemplate;
    private NearbyCompsService service;

    @BeforeEach
    void setUp() {
        jdbcTemplate = mock(JdbcTemplate.class);
        service = new NearbyCompsService(jdbcTemplate, new ObjectMapper());
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    private TargetParcel makeTarget() {
        return new TargetParcel(
                "target-1", TARGET_WKT, null,
                "10 Elm St, Nashua, NH",
                0.25,       // acres
                200_000.0,  // assessedValue
                PropertyType.SINGLE_FAMILY
        );
    }

    /**
     * Stubs jdbcTemplate.query to invoke the RowCallbackHandler once per
     * supplied ResultSet — no real database involved.
     */
    private void stubRows(ResultSet... rows) throws SQLException {
        doAnswer(inv -> {
            RowCallbackHandler handler = inv.getArgument(1);
            for (ResultSet rs : rows) {
                handler.processRow(rs);
            }
            return null;
        }).when(jdbcTemplate).query(anyString(), any(RowCallbackHandler.class), any(Object[].class));
    }

    /**
     * Builds a minimal mock ResultSet for one candidate row.
     * Only stub the fields each test is asserting on — leave everything else
     * at Mockito's default (null / false / 0).
     */
    private ResultSet row(
            String propertyId,
            String address,
            String geomWkt,
            double distanceM,
            boolean touches,
            Double acreage,
            String rawPropertyType) throws SQLException {

        ResultSet rs = mock(ResultSet.class);
        when(rs.getString("property_id")).thenReturn(propertyId);
        when(rs.getString("address")).thenReturn(address);
        when(rs.getString("city")).thenReturn("Nashua");
        when(rs.getString("property_type")).thenReturn(rawPropertyType);
        when(rs.getObject("assessed_value")).thenReturn(null);
        when(rs.getObject("acreage")).thenReturn(acreage);
        when(rs.getDouble("distance_m")).thenReturn(distanceM);
        when(rs.getDouble("latitude")).thenReturn(43.0003);
        when(rs.getDouble("longitude")).thenReturn(-71.5015);
        when(rs.getBoolean("touches")).thenReturn(touches);
        when(rs.getString("geom_wkt")).thenReturn(geomWkt);
        when(rs.getString("geom_geojson")).thenReturn(null);
        return rs;
    }

    // -------------------------------------------------------------------------
    // foundVia assignment
    // -------------------------------------------------------------------------

    @Test
    void foundVia_geometric_whenCandidateTouchesTarget() throws SQLException {
        stubRows(row("comp-1", "8 Elm St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 0.25, "Single Family"));

        List<CompCandidate> result = service.findComps(makeTarget(), false);

        assertEquals(1, result.size());
        assertTrue(result.get(0).getFoundVia().contains("geometric"));
    }

    @Test
    void foundVia_nearSimilarSize_whenWithinCloseRadiusAndSimilarAcreage() throws SQLException {
        // 50 m away, same lot size — qualifies on proximity + size alone (not touching)
        stubRows(row("comp-2", "12 Elm St, Nashua, NH", ADJACENT_WKT,
                50.0, false, 0.25, "Single Family"));

        List<CompCandidate> result = service.findComps(makeTarget(), false);

        assertEquals(1, result.size());
        assertTrue(result.get(0).getFoundVia().contains("near_similar_size"));
    }

    @Test
    void foundVia_nearSameStreet_qualifiesWhenLotSizeAlsoSimilar() throws SQLException {
        // 200 m away, same street name, similar lot size → included via near_same_street
        stubRows(row("comp-3", "40 Elm St, Nashua, NH", ADJACENT_WKT,
                200.0, false, 0.30, "Single Family"));

        List<CompCandidate> result = service.findComps(makeTarget(), false);

        assertEquals(1, result.size());
        assertTrue(result.get(0).getFoundVia().contains("near_same_street"));
    }

    @Test
    void foundVia_sameStreet_stillExcludedWhenFinalLotSizeCheckFails() throws SQLException {
        // near_same_street doesn't itself check lot size — but the final filter does.
        // Ratio: 2.0 / 0.25 = 8.0 > 2.5 default tolerance → removed.
        stubRows(row("comp-4", "40 Elm St, Nashua, NH", ADJACENT_WKT,
                200.0, false, 2.0, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty(),
                "dissimilar lot size should fail the final filter even when same street");
    }

    // -------------------------------------------------------------------------
    // Exclusion rules
    // -------------------------------------------------------------------------

    @Test
    void excludes_candidateWithBlankAddress() throws SQLException {
        stubRows(row("comp-blank", "   ", ADJACENT_WKT,
                3.0, true, 0.25, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void excludes_candidateWithNullAddress() throws SQLException {
        stubRows(row("comp-null-addr", null, ADJACENT_WKT,
                3.0, true, 0.25, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void excludes_candidateWithDissimilarLotSize() throws SQLException {
        // touches=true → gets into foundVia, but final lotSizeSimilar check removes it
        // Ratio: 2.5 / 0.25 = 10.0 > 2.5 tolerance
        stubRows(row("comp-bigLot", "8 Oak St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 2.5, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void excludes_ineligiblePropertyType_commercial() throws SQLException {
        // Non-null rawPropertyType that doesn't map to SINGLE_FAMILY or VACANT_LAND
        stubRows(row("comp-commercial", "8 Oak St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 0.25, "Commercial"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void excludes_ineligiblePropertyType_multiFamily() throws SQLException {
        stubRows(row("comp-mf", "8 Oak St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 0.25, "Multi Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void keeps_candidateWithNullPropertyType_asUnverified() throws SQLException {
        // null rawPropertyType = assessor data missing.
        // Kept and tagged compEligible=null rather than excluded.
        stubRows(row("comp-unknown", "8 Oak St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 0.25, null));

        List<CompCandidate> result = service.findComps(makeTarget(), false);
        assertEquals(1, result.size());
        assertNull(result.get(0).getCompEligible(),
                "null property type should yield compEligible=null, not false");
    }

    @Test
    void keeps_vacantLandCandidate() throws SQLException {
        stubRows(row("comp-land", "8 Oak St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 0.25, "Vacant Land"));

        assertEquals(1, service.findComps(makeTarget(), false).size());
    }

    @Test
    void excludes_candidateWithNoFoundVia() throws SQLException {
        // Not touching, beyond close radius, different street name → foundVia empty → excluded
        stubRows(row("comp-nothing", "8 Oak St, Nashua, NH", ADJACENT_WKT,
                200.0, false, 0.25, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    // -------------------------------------------------------------------------
    // Geometry screens (run in pure Java via JTS — no PostGIS needed)
    // -------------------------------------------------------------------------

    @Test
    void excludes_geometryWhoseFurthestVertexExceeds500m() throws SQLException {
        // DISTANT_WKT is ~40 km away from the target centroid
        stubRows(row("comp-distant-geom", "8 Elm St, Nashua, NH", DISTANT_WKT,
                3.0, true, 0.25, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void excludes_geometryThatIsNotAPolygon() throws SQLException {
        // A bare Point — not a Polygon or MultiPolygon → discarded
        stubRows(row("comp-point", "8 Elm St, Nashua, NH",
                "POINT(-71.5015 43.0003)",
                3.0, true, 0.25, "Single Family"));

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void keeps_candidateWhoseGeomWktIsNull() throws SQLException {
        // null geom_wkt → candidateGeom stays null → geometry block is skipped entirely,
        // candidate is still evaluated on its other fields
        stubRows(row("comp-nogeom", "8 Elm St, Nashua, NH", null,
                3.0, true, 0.25, "Single Family"));

        assertEquals(1, service.findComps(makeTarget(), false).size());
    }

    // -------------------------------------------------------------------------
    // Edge / happy-path cases
    // -------------------------------------------------------------------------

    @Test
    void returnsEmpty_whenDatabaseReturnsNoRows() throws SQLException {
        stubRows(); // zero rows

        assertTrue(service.findComps(makeTarget(), false).isEmpty());
    }

    @Test
    void returnsMultipleCandidates_whenSeveralQualify() throws SQLException {
        ResultSet row1 = row("comp-a", "8 Elm St, Nashua, NH", ADJACENT_WKT,
                3.0, true, 0.25, "Single Family");
        ResultSet row2 = row("comp-b", "12 Elm St, Nashua, NH", ADJACENT_WKT,
                50.0, false, 0.30, "Single Family");
        stubRows(row1, row2);

        assertEquals(2, service.findComps(makeTarget(), false).size());
    }

    @Test
    void invalidTargetWkt_returnsEmptyImmediately_withoutCallingDatabase() throws SQLException {
        // WKT parse failure short-circuits before jdbcTemplate is ever called
        TargetParcel badTarget = new TargetParcel(
                "bad", "NOT VALID WKT", null,
                "10 Elm St, Nashua, NH", 0.25, null, PropertyType.SINGLE_FAMILY);

        List<CompCandidate> result = service.findComps(badTarget, false);

        assertTrue(result.isEmpty());
        verify(jdbcTemplate, never())
                .query(anyString(), any(RowCallbackHandler.class), any(Object[].class));
    }
}
