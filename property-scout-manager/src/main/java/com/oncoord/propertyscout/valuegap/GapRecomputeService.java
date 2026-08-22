package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.Listing;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * Replaces GapAnalysisJobService/GapAnalysisJob for this project's actual
 * needs. Recompute is a manual, background, no-one's-watching admin action
 * now -- not a live user-facing request -- so it doesn't need parallelism,
 * a job-state object, or a poll endpoint. A plain sequential loop over a
 * synchronous HTTP request is simpler, easier to debug (a failure points
 * straight at the listing/SQL that caused it, not a job-status blob), and
 * completely adequate: even a full CT run (4,571 listings) finishes in
 * minutes sequentially, which is fine for something nobody's blocked on.
 *
 * Two responsibilities: recomputeAndStore() (write path, POST /recompute)
 * and findRanked() (read path, GET /rank) -- both against gap_results.
 */
@Service
public class GapRecomputeService {

    private final ValueGapPipelineService pipelineService;
    private final GapRankingService gapRankingService;
    private final JdbcTemplate jdbcTemplate;

    private static final String UPSERT_SQL = """
            INSERT INTO gap_results
                (listing_id, has_comps, target_assessed_value, comp_median, comp_min, comp_max,
                 comp_count, comp_property_ids, gap, gap_pct, relative_gap_pct, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            ON CONFLICT (listing_id) DO UPDATE SET
                has_comps = EXCLUDED.has_comps,
                target_assessed_value = EXCLUDED.target_assessed_value,
                comp_median = EXCLUDED.comp_median,
                comp_min = EXCLUDED.comp_min,
                comp_max = EXCLUDED.comp_max,
                comp_count = EXCLUDED.comp_count,
                comp_property_ids = EXCLUDED.comp_property_ids,
                gap = EXCLUDED.gap,
                gap_pct = EXCLUDED.gap_pct,
                relative_gap_pct = EXCLUDED.relative_gap_pct,
                computed_at = EXCLUDED.computed_at
            """;

    public GapRecomputeService(ValueGapPipelineService pipelineService, GapRankingService gapRankingService,
                               JdbcTemplate jdbcTemplate) {
        this.pipelineService = pipelineService;
        this.gapRankingService = gapRankingService;
        this.jdbcTemplate = jdbcTemplate;
    }

    public static class RecomputeSummary {
        public final int totalListings;
        public final int computed;
        public final int hasComps;

        public RecomputeSummary(int totalListings, int computed, int hasComps) {
            this.totalListings = totalListings;
            this.computed = computed;
            this.hasComps = hasComps;
        }
    }

    /**
     * Blocks until every listing is processed -- simple, sequential,
     * one at a time. Returns a plain summary; nothing to poll.
     */
    public RecomputeSummary recomputeAndStore(List<Listing> listings) {
        List<GapResult> results = new ArrayList<>();
        for (Listing listing : listings) {
            pipelineService.computeForListing(listing).ifPresent(results::add);
        }

        // rank() sets relativeGapPct on each result as a side effect
        // (group-relative to gapPct within its property type) -- the
        // grouped/sorted return value itself isn't needed here (findRanked
        // does its own grouping at read time straight from the DB), but
        // without this call relativeGapPct silently stays null forever,
        // since nothing else ever calls the setter. That's exactly what
        // was happening before this fix -- relative_gap_pct has been NULL
        // for every persisted row.
        gapRankingService.rank(results);

        int hasCompsCount = (int) results.stream().filter(GapResult::isHasComps).count();
        persistResults(results);

        return new RecomputeSummary(listings.size(), results.size(), hasCompsCount);
    }

    private void persistResults(List<GapResult> results) {
        if (results.isEmpty()) {
            return;
        }
        jdbcTemplate.execute((ConnectionCallback<Void>) con -> {
            List<Object[]> rows = new ArrayList<>();
            for (GapResult r : results) {
                List<CompCandidate> comps = r.getComps();
                Integer compCount = comps != null ? comps.size() : null;
                List<String> compPropertyIds = comps != null
                        ? comps.stream().map(CompCandidate::getPropertyId).collect(Collectors.toList())
                        : null;
                rows.add(new Object[]{
                        r.getListingId(),
                        r.isHasComps(),
                        r.getTargetAssessedValue(),
                        r.getCompMedian(),
                        r.getCompMin(),
                        r.getCompMax(),
                        compCount,
                        toSqlArray(con, compPropertyIds),
                        r.getGap(),
                        r.getGapPct(),
                        r.getRelativeGapPct(),
                });
            }
            jdbcTemplate.batchUpdate(UPSERT_SQL, rows);
            return null;
        });
    }

    private static java.sql.Array toSqlArray(Connection con, List<String> ids) throws java.sql.SQLException {
        if (ids == null || ids.isEmpty()) {
            return null;
        }
        return con.createArrayOf("text", ids.toArray());
    }

    /**
     * Fast read path for GET /rank -- pure SQL against precomputed
     * gap_results, no live computation. Groups by property_type (already
     * sorted gap DESC by the query) and truncates each group to `limit` if
     * given.
     */
    public Map<String, Object> findRanked(String state, String city, String zipCode,
                                          String propertyType, Integer limit) {
        String sql = """
                SELECT l.listing_id, l.formatted_address AS address, l.property_type,
                       l.year_built, l.price, g.target_assessed_value, g.comp_median,
                       g.comp_min, g.comp_max, g.comp_count, g.gap, g.gap_pct, g.relative_gap_pct
                FROM gap_results g
                JOIN listings l ON l.listing_id = g.listing_id
                WHERE l.state = ?
                  AND (?::text IS NULL OR l.city = ?)
                  AND (?::text IS NULL OR l.zip_code = ?)
                  AND (?::text IS NULL OR l.property_type = ?)
                  AND g.has_comps = true
                ORDER BY l.property_type, g.gap DESC
                """;
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(sql,
                state, city, city, zipCode, zipCode, propertyType, propertyType);

        Map<String, List<Map<String, Object>>> byType = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String type = (String) row.get("property_type");
            List<Map<String, Object>> group = byType.computeIfAbsent(type, k -> new ArrayList<>());
            if (limit == null || group.size() < limit) {
                group.add(row);
            }
        }
        return Map.of("rankedByPropertyType", byType);
    }
}