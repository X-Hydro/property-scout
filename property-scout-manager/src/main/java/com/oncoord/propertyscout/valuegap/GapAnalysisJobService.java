package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.Listing;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.sql.Connection;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Runs a gap-ranking batch as a background job with live, pollable progress,
 * instead of blocking one long synchronous HTTP request (a full CT run
 * measured ~13 min for CT sequentially -- 0.175s/listing x 4,571 listings --
 * far too long for a single request/response, and far longer than it needs
 * to be, since every listing's comp search is independent of every other
 * listing's).
 *
 * DELIBERATELY reimplements ValueGapPipelineService.rankListings()'s loop
 * (computeForListing() per listing, then GapRankingService.rank() +
 * applyLimit() over the results) rather than calling rankListings() as one
 * opaque batch call -- confirmed against the real
 * ValueGapPipelineService.java that this is exactly what rankListings()
 * does internally, so behavior is unchanged; doing the loop here is what
 * makes both progress tracking AND parallelism possible, neither of which
 * rankListings() itself supports.
 *
 * PARALLELISM: listings are processed with up to LISTING_PARALLELISM
 * concurrent comp-search calls (each computeForListing() call does its own
 * DB round trip via NearbyCompsService), rather than one at a time -- the
 * real lever for reducing wall-clock time, not just narrating the same
 * sequential wait. Bounded deliberately: unbounded parallelism would let
 * one statewide run flood the DB connection pool. LISTING_PARALLELISM
 * should be tuned to (and stay comfortably under) the app's real Hikari
 * max-pool-size -- 8 is a starting guess, not a measured value, since this
 * project's actual pool size wasn't available when this was written.
 *
 * JOB_PARALLELISM bounds how many statewide runs can be in flight at once.
 * Total possible concurrent DB queries is JOB_PARALLELISM x
 * LISTING_PARALLELISM -- tune both together, not just one, against the
 * real connection pool size.
 *
 * CITY-LEVEL PROGRESS: the frontend only wants to show "processing city N
 * of M", not a listing count, per the simpler UX Thale asked for. But
 * listings are still processed in parallel across cities, not city by
 * city -- so "a city is done" has to be detected as "this was that city's
 * LAST remaining listing", not inferred from ordering. remainingPerCity
 * tracks how many listings are still outstanding for each city; a listing
 * finishing decrements its city's count, and whichever thread happens to
 * bring a city's count to exactly zero (guaranteed to be exactly one
 * thread, since AtomicInteger.decrementAndGet() is atomic) is the one that
 * reports that city complete.
 *
 * In-memory job store: fine for a single-instance deployment (matches this
 * project's current single-VM Docker Compose setup). Would need a shared
 * store (DB row, Redis) instead behind multiple app instances/load
 * balancing, since a poll could otherwise land on an instance that never
 * started the job.
 */
@Service
public class GapAnalysisJobService {

    private static final int LISTING_PARALLELISM = 8; // tune to Hikari max-pool-size
    private static final int JOB_PARALLELISM = 2;      // concurrent statewide runs allowed at once

    private final ValueGapPipelineService pipelineService;
    private final GapRankingService gapRankingService;
    private final JdbcTemplate jdbcTemplate;
    private final Map<String, GapAnalysisJob> jobs = new ConcurrentHashMap<>();

    private final ExecutorService jobExecutor = Executors.newFixedThreadPool(JOB_PARALLELISM);

    // Pivot: this job service now doubles as the RECOMPUTE engine, run
    // manually after a listings/property_values refresh -- not triggered
    // live by an investor clicking Generate. /api/value-gap/rank reads
    // gap_results directly instead of calling computeForListing on demand,
    // so results here get persisted, not just returned in the job payload.
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

    public GapAnalysisJobService(ValueGapPipelineService pipelineService, GapRankingService gapRankingService,
                                 JdbcTemplate jdbcTemplate) {
        this.pipelineService = pipelineService;
        this.gapRankingService = gapRankingService;
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Starts the batch running in the background and returns immediately
     * with a job id to poll. NOT run via Spring's @Async -- self-invocation
     * from within the same bean would bypass the AOP proxy @Async depends
     * on and silently run synchronously, defeating the whole point. An
     * explicit ExecutorService submit avoids that trap entirely.
     */
    public String startJob(List<Listing> listings, Integer limit) {
        // Sort by city before processing -- NOT just cosmetic. Listings
        // come back from the DB in whatever order the query happens to
        // return them (not grouped by city), and a city only counts as
        // "processed" once its LAST listing finishes. Left unsorted, a
        // city's listings are scattered essentially randomly across the
        // whole list, so almost no city finishes until the run is nearly
        // done -- confirmed via simulation: unsorted, only ~3% of cities
        // are done at 50% of listings processed; sorted, ~52% are. Sorting
        // clusters each city's listings together in processing order, so
        // completions trickle in roughly proportionally instead of all
        // landing in a rush at the very end.
        //
        // A null city (shouldn't happen with real RentCast data, but not
        // guaranteed) is mapped to a literal "(Unknown)" bucket rather than
        // left as null -- ConcurrentHashMap disallows null keys outright
        // and would throw here otherwise, taking down the whole job over
        // one bad record instead of just grouping it oddly.
        List<Listing> sorted = new ArrayList<>(listings);
        sorted.sort(Comparator.comparing(l -> l.getCity() != null ? l.getCity() : "(Unknown)"));

        Map<String, AtomicInteger> remainingPerCity = new ConcurrentHashMap<>();
        for (Listing listing : sorted) {
            String city = listing.getCity() != null ? listing.getCity() : "(Unknown)";
            remainingPerCity.computeIfAbsent(city, k -> new AtomicInteger(0)).incrementAndGet();
        }

        String jobId = UUID.randomUUID().toString();
        GapAnalysisJob job = new GapAnalysisJob(sorted.size(), remainingPerCity.size());
        jobs.put(jobId, job);
        jobExecutor.submit(() -> runJob(job, sorted, limit, remainingPerCity));
        return jobId;
    }

    public GapAnalysisJob getJob(String jobId) {
        return jobs.get(jobId);
    }

    /**
     * Fast read path for GET /rank -- pure SQL against precomputed
     * gap_results, no NearbyCompsService/GapComputationService call. Groups
     * by property_type (already sorted gap DESC by the query) and truncates
     * each group to `limit` if given, same semantics as
     * GapRankingService.applyLimit had for the old live path -- but this no
     * longer touches GapResult at all, since results are already flat rows.
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
                  AND (? IS NULL OR l.city = ?)
                  AND (? IS NULL OR l.zip_code = ?)
                  AND (? IS NULL OR l.property_type = ?)
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

    public void requestCancel(String jobId) {
        GapAnalysisJob job = jobs.get(jobId);
        if (job != null) {
            job.requestCancel();
        }
    }

    private void runJob(GapAnalysisJob job, List<Listing> listings, Integer limit,
                        Map<String, AtomicInteger> remainingPerCity) {
        // Own worker pool per job run (not the same pool startJob's task
        // runs on) -- sized to LISTING_PARALLELISM, shut down at the end of
        // this run rather than shared/reused across jobs, so one job's
        // listing-level concurrency never bleeds into another's.
        ExecutorService listingWorkers =
                Executors.newFixedThreadPool(Math.max(1, Math.min(LISTING_PARALLELISM, listings.size())));
        try {
            List<GapResult> results = Collections.synchronizedList(new ArrayList<>());

            List<CompletableFuture<Void>> futures = new ArrayList<>(listings.size());
            for (Listing listing : listings) {
                futures.add(CompletableFuture.runAsync(() -> {
                    if (job.isCancelRequested()) return;
                    pipelineService.computeForListing(listing).ifPresent(results::add);
                    job.recordProgress(listing.getCity());

                    AtomicInteger remaining = remainingPerCity.get(
                            listing.getCity() != null ? listing.getCity() : "(Unknown)");
                    if (remaining != null && remaining.decrementAndGet() == 0) {
                        job.recordCityComplete();
                    }
                }, listingWorkers));
            }
            // Wait for every listing's comp search to finish before ranking
            // -- ranking needs the complete result set (see
            // GapRankingService.applyLimit's javadoc for why the top-N
            // truncation can't happen before every listing is scored).
            CompletableFuture.allOf(futures.toArray(new CompletableFuture[0])).join();

            if (!job.isCancelRequested()) {
                persistResults(new ArrayList<>(results));
            }

            GapRankingService.RankedGaps ranked = gapRankingService.rank(new ArrayList<>(results));
            Map<String, List<GapResult>> limited =
                    GapRankingService.applyLimit(ranked.getRankedByPropertyType(), limit);

            job.complete(Map.of(
                    "rankedByPropertyType", limited,
                    "noComps", ranked.getNoComps()
            ));
        } catch (Exception e) {
            job.fail(e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName());
        } finally {
            listingWorkers.shutdown();
        }
    }

    /**
     * Field access below is now CONFIRMED against the real GapResult.java
     * and CompCandidate.java: getListingId(), isHasComps(),
     * getTargetAssessedValue(), getCompMedian(), getCompMin(), getCompMax(),
     * getGap(), getGapPct(), getRelativeGapPct() are all real.
     *
     * comp_property_ids is deliberately left null for now (an empty/absent
     * java.sql.Array, same as any other null column -- binds fine as null
     * either way). getComps() returns List<Double> -- the raw assessed
     * VALUES used in the median -- with no link back to which
     * CompCandidate (and therefore which property_id) each value came
     * from. getCandidates() has the property_ids but is the BROADER
     * pre-filter list, not the exact subset that fed the median.
     * Reverse-matching candidates to comps by assessedValue would be
     * unreliable (two different properties can share the same municipal
     * assessment) for a column whose whole point is being a trustworthy
     * audit trail, so this isn't populated until GapComputationService is
     * changed to preserve that link directly (e.g. building comps as
     * List<CompCandidate> instead of List<Double>). Once real ids are
     * available, build them the same way toSqlArray() below already does
     * -- just replace the `null` with e.g. toSqlArray(con, realIdList).
     *
     * Runs inside a ConnectionCallback (not the plain
     * jdbcTemplate.batchUpdate(sql, List<Object[]>) used elsewhere in this
     * file) because comp_property_ids is a native Postgres TEXT[] column:
     * a raw Java String[] can't just be bound as an Object -- JDBC needs a
     * real java.sql.Array, built via Connection.createArrayOf(), which
     * requires direct Connection access.
     */
    private void persistResults(List<GapResult> results) {
        if (results.isEmpty()) {
            return;
        }
        jdbcTemplate.execute((ConnectionCallback<Void>) con -> {
            List<Object[]> rows = new ArrayList<>();
            for (GapResult r : results) {
                List<Double> comps = r.getComps();
                Integer compCount = comps != null ? comps.size() : null;
                rows.add(new Object[]{
                        r.getListingId(),
                        r.isHasComps(),
                        r.getTargetAssessedValue(),
                        r.getCompMedian(),
                        r.getCompMin(),
                        r.getCompMax(),
                        compCount,
                        toSqlArray(con, null), // comp_property_ids -- see javadoc above
                        r.getGap(),
                        r.getGapPct(),
                        r.getRelativeGapPct(),
                });
            }
            jdbcTemplate.batchUpdate(UPSERT_SQL, rows);
            return null;
        });
    }

    /** Null-safe: a null/empty id list binds as a real SQL NULL, not an empty array. */
    private static java.sql.Array toSqlArray(Connection con, List<String> ids) throws java.sql.SQLException {
        if (ids == null || ids.isEmpty()) {
            return null;
        }
        return con.createArrayOf("text", ids.toArray());
    }
}