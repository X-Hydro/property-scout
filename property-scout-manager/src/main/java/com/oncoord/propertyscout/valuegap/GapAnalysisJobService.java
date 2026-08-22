package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.Listing;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
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
    private final Map<String, GapAnalysisJob> jobs = new ConcurrentHashMap<>();

    private final ExecutorService jobExecutor = Executors.newFixedThreadPool(JOB_PARALLELISM);

    public GapAnalysisJobService(ValueGapPipelineService pipelineService, GapRankingService gapRankingService) {
        this.pipelineService = pipelineService;
        this.gapRankingService = gapRankingService;
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
}