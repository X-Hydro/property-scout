package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.Listing;
import com.oncoord.propertyscout.service.ListingsService;
import com.oncoord.propertyscout.valuegap.GapAnalysisJob;
import com.oncoord.propertyscout.valuegap.GapAnalysisJobService;
import com.oncoord.propertyscout.valuegap.GapRankingService;
import com.oncoord.propertyscout.valuegap.GapResult;
import com.oncoord.propertyscout.valuegap.ValueGapPipelineService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/value-gap")
public class ValueGapController {

    private static final Logger log = LoggerFactory.getLogger(ValueGapController.class);

    private final ListingsService listingsService;
    private final ValueGapPipelineService pipelineService;
    private final GapAnalysisJobService jobService;

    public ValueGapController(ListingsService listingsService, ValueGapPipelineService pipelineService,
                              GapAnalysisJobService jobService) {
        this.listingsService = listingsService;
        this.pipelineService = pipelineService;
        this.jobService = jobService;
    }

    /** Equivalent of `python compute_gap.py combined.geojson "<address>"` for one listing, live. */
    @GetMapping("/listings/{listingId}")
    public ResponseEntity<GapResult> getGapForListing(@PathVariable String listingId) {
        return listingsService.findById(listingId)
                .flatMap(pipelineService::computeForListing)
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    /**
     * Equivalent of `python compute_gap.py combined.geojson --rank ranked.csv`,
     * scoped to a state (and optionally city/zip/propertyType) instead of a
     * single town's combined.geojson file.
     *
     * SYNCHRONOUS -- blocks until every matching listing is processed. Fine
     * for a single city (small, fast). For a statewide run (no city,
     * potentially thousands of listings -- measured ~13 min for CT at
     * ~0.175s/listing x 4,571 listings), use POST /rank/jobs +
     * GET /rank/jobs/{jobId} instead, which reports live progress rather
     * than holding one request open for the whole run.
     *
     * limit is optional and applied per property type, AFTER ranking (see
     * GapRankingService.applyLimit's javadoc for why it can't be applied
     * any earlier) -- e.g. a state-only query (no city) with limit=25
     * returns the top 25 by gap for each property type across the whole
     * state, for a "biggest gaps, don't care what town" view, rather than
     * every matching listing statewide.
     */
    @GetMapping("/rank")
    public Map<String, Object> rankGaps(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType,
            @RequestParam(required = false) Integer limit) {

        long startedAt = System.currentTimeMillis();
        List<Listing> listings = listingsService.findListings(state, city, zipCode, propertyType);
        log.info("Synchronous /rank: state={} city={} zipCode={} propertyType={} limit={} -> {} listings",
                state, city, zipCode, propertyType, limit, listings.size());

        GapRankingService.RankedGaps ranked = pipelineService.rankListings(listings);
        Map<String, List<GapResult>> rankedByPropertyType =
                GapRankingService.applyLimit(ranked.getRankedByPropertyType(), limit);

        long elapsedMs = System.currentTimeMillis() - startedAt;
        log.info("Synchronous /rank completed in {} ms for state={} city={} ({} listings)",
                elapsedMs, state, city, listings.size());

        return Map.of(
                "rankedByPropertyType", rankedByPropertyType,
                "noComps", ranked.getNoComps()
        );
    }

    /**
     * ASYNC counterpart to /rank: starts the same computation as a
     * background job (see GapAnalysisJobService) and returns immediately
     * with a jobId, instead of blocking until every listing is processed.
     * Poll GET /rank/jobs/{jobId} for live progress and the final result.
     * Same query params as /rank.
     */
    @PostMapping("/rank/jobs")
    public Map<String, String> startRankJob(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType,
            @RequestParam(required = false) Integer limit) {

        List<Listing> listings = listingsService.findListings(state, city, zipCode, propertyType);
        log.info("Async /rank/jobs request: state={} city={} zipCode={} propertyType={} limit={} -> {} listings",
                state, city, zipCode, propertyType, limit, listings.size());
        String jobId = jobService.startJob(listings, limit);
        return Map.of("jobId", jobId);
    }

    /**
     * Poll target for a job started via POST /rank/jobs. While RUNNING,
     * returns processed/total/currentCity only. Once COMPLETED, also
     * includes "result" (same shape /rank returns directly). Once FAILED,
     * includes "error". 404 if jobId is unknown (never existed -- there's
     * no TTL/eviction yet, so an id that once existed stays valid for the
     * life of the server process).
     *
     * Deliberately NOT logged -- the frontend polls this every ~1s per
     * running job (see JOB_POLL_INTERVAL_MS in property-scout.js), so
     * logging every call here would drown out everything else in the logs
     * for the whole duration of a run.
     */
    @GetMapping("/rank/jobs/{jobId}")
    public ResponseEntity<Map<String, Object>> getRankJobStatus(@PathVariable String jobId) {
        GapAnalysisJob job = jobService.getJob(jobId);
        if (job == null) {
            return ResponseEntity.notFound().build();
        }

        Map<String, Object> body = new HashMap<>();
        body.put("status", job.getStatus().name());
        body.put("processed", job.getProcessed());
        body.put("total", job.getTotal());
        body.put("citiesProcessed", job.getCitiesProcessed());
        body.put("totalCities", job.getTotalCities());
        body.put("currentCity", job.getCurrentCity());
        if (job.getStatus() == GapAnalysisJob.Status.COMPLETED) {
            body.put("result", job.getResult());
        } else if (job.getStatus() == GapAnalysisJob.Status.FAILED) {
            body.put("error", job.getErrorMessage());
        }
        return ResponseEntity.ok(body);
    }

    @PostMapping("/rank/jobs/{jobId}/cancel")
    public void cancelRankJob(@PathVariable String jobId) {
        jobService.requestCancel(jobId);
    }
}