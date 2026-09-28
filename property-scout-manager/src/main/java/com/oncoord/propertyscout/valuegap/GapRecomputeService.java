package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.Listing;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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
    private static final Logger log = LoggerFactory.getLogger(GapRecomputeService.class);

    private static final List<String> DEFAULT_STATUSES = List.of("Active");

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
        int total = listings.size();
        int processed = 0;
        int failed = 0;
        for (Listing listing : listings) {
            try {
                pipelineService.computeForListing(listing).ifPresent(results::add);
            } catch (Exception e) {
                log.warn("computeForListing failed for listing {}: {}", listing.getListingId(), e.getMessage());
                failed++;
            }
            processed++;
            if (processed % 1000 == 0 || processed == total) {
                log.info("Processed {} of {} records ({} failed)", processed, total, failed);
            }
        }
        log.info("Processed {} of {} records ({} failed)", processed, total, failed);

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

    public Map<String, Object> findRanked(String state, String city, String zipCode,
                                          String propertyType, Double maxPrice, Integer limit, List<String> statuses) {
        List<String> effectiveStatuses = (statuses == null || statuses.isEmpty())
                ? DEFAULT_STATUSES
                : statuses;
        String statusPlaceholders = effectiveStatuses.stream()
                .map(s -> "?")
                .collect(Collectors.joining(", "));

        String sql = ("""
                SELECT l.listing_id, l.formatted_address AS address, l.property_type,
                       l.year_built, l.price, l.status, g.target_assessed_value, g.comp_median,
                       g.comp_min, g.comp_max, g.comp_count, g.gap, g.gap_pct, g.relative_gap_pct,
                       l.square_footage AS target_sqft,
                       s.sqft_comp_count,
                       s.comp_median_price_per_sqft,
                       s.comp_median_price_per_sqft * l.square_footage AS sqft_adjusted_comp_value,
                       s.comp_median_price_per_sqft * l.square_footage - l.price AS sqft_adjusted_gap,
                       (s.comp_median_price_per_sqft * l.square_footage - l.price)
                           / NULLIF(l.price, 0) * 100.0 AS sqft_adjusted_gap_pct
                FROM gap_results g
                JOIN listings l ON l.listing_id = g.listing_id
                -- Square-footage-adjusted comparison, computed at read time from the comps
                -- gap_results already recorded (comp_property_ids) -- nothing extra is stored,
                -- and it reflects property_values' CURRENT building_sqft, so a sqft backfill
                -- shows up without a recompute. Mirrors GapComputationService.applySqftAdjustment:
                -- median $/sqft of comps with plausible sqft (400-20000), scaled by the listing's
                -- own square_footage; NULL (never a fallback) when the listing is Land, has no
                -- plausible sqft, or fewer than 3 comps have usable sqft. Keep the 400 / 20000 / 3
                -- constants in sync with that class. percentile_cont(0.5) averages the two middle
                -- values on an even count, same as the Java median.
                LEFT JOIN LATERAL (
                    SELECT count(*) AS sqft_comp_count,
                           percentile_cont(0.5) WITHIN GROUP
                               (ORDER BY pv.assessed_value / pv.building_sqft) AS comp_median_price_per_sqft
                    FROM property_values pv
                    WHERE pv.property_id = ANY(g.comp_property_ids)
                      AND pv.assessed_value IS NOT NULL
                      AND pv.building_sqft BETWEEN 400 AND 20000
                      AND l.square_footage BETWEEN 400 AND 20000
                      AND l.price > 0
                      AND l.property_type IS DISTINCT FROM 'Land'
                    HAVING count(*) >= 3
                ) s ON true
                WHERE l.state = ?
                  AND (?::text IS NULL OR l.city = ?)
                  AND (?::text IS NULL OR l.zip_code = ?)
                  AND (?::text IS NULL OR l.property_type = ?)
                  AND (?::numeric IS NULL OR l.price <= ?)
                  AND l.status IN (%s)
                  AND g.has_comps = true
                  -- Under 3 comps is too thin to rank: one outlier comp (a full-size house
                  -- next to a trailer, say) produced gaps in the thousands of percent that
                  -- then sat at the very top of the list.
                  AND g.comp_count >= 3
                -- Listings WITH a sqft-adjusted gap come first (largest adjusted gap percent
                -- first), then listings without one by raw gap. The browser sorts the same
                -- way, and the statewide top-N cut happens on THIS order, so the two must
                -- stay identical.
                ORDER BY l.property_type,
                         (s.comp_median_price_per_sqft IS NULL),
                         ((s.comp_median_price_per_sqft * l.square_footage - l.price)
                             / NULLIF(l.price, 0)) DESC NULLS LAST,
                         g.gap DESC NULLS LAST
                """).formatted(statusPlaceholders);

        List<Object> args = new ArrayList<>();
        args.add(state);
        args.add(city);
        args.add(city);
        args.add(zipCode);
        args.add(zipCode);
        args.add(propertyType);
        args.add(propertyType);
        args.add(maxPrice);
        args.add(maxPrice);
        args.addAll(effectiveStatuses);

        List<Map<String, Object>> rows = jdbcTemplate.queryForList(sql, args.toArray());

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