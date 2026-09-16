package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.Listing;
import com.oncoord.propertyscout.service.ListingsService;
import com.oncoord.propertyscout.valuegap.GapRecomputeService;
import com.oncoord.propertyscout.valuegap.GapResult;
import com.oncoord.propertyscout.valuegap.ValueGapPipelineService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/value-gap")
public class ValueGapController {

    private static final Logger log = LoggerFactory.getLogger(ValueGapController.class);

    private final ListingsService listingsService;
    private final ValueGapPipelineService pipelineService;
    private final GapRecomputeService recomputeService;

    public ValueGapController(ListingsService listingsService, ValueGapPipelineService pipelineService,
                              GapRecomputeService recomputeService) {
        this.listingsService = listingsService;
        this.pipelineService = pipelineService;
        this.recomputeService = recomputeService;
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
     * Reads precomputed results from gap_results (see gap_results_schema.sql)
     * -- fast, DB-only, no live comp computation. Results only exist for
     * listings a recompute has actually run for (POST /recompute); anything
     * not yet computed simply won't appear here.
     *
     * limit is optional, applied per property type -- e.g. a state-only
     * query (no city) with limit=25 returns the top 25 by gap for each
     * property type across the whole state, for a "biggest gaps, don't care
     * what town" view, rather than every matching listing statewide.
     *
     * maxPrice is optional and is applied in the WHERE clause, BEFORE limit
     * truncation -- e.g. limit=25&maxPrice=1000000 returns the top 25 by gap
     * among listings priced at or below $1M, not the top 25 overall with
     * anything over $1M stripped out afterward (which could leave fewer
     * than 25, or zero, results if the biggest-gap listings all happen to
     * be expensive). This only affects what /rank returns -- gap_results
     * itself is still computed for every listing regardless of price via
     * POST /recompute, so nothing is lost, just filtered at read time.
     *
     * statuses is optional, comma-separated (e.g. statuses=Active,Pending)
     * -- Spring binds a repeated/comma-separated query param straight to
     * List<String>. Defaults to Active-only when omitted (see
     * GapRecomputeService.DEFAULT_STATUSES) -- the frontend's "include
     * pending/contingent" toggle sets this explicitly to widen the result.
     */
    @GetMapping("/rank")
    public ResponseEntity<?> rankGaps(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType,
            @RequestParam(required = false) Double maxPrice,
            @RequestParam(required = false) Integer limit,
            @RequestParam(required = false) List<String> statuses) {

        try {
            return ResponseEntity.ok(recomputeService.findRanked(state, city, zipCode, propertyType, maxPrice, limit, statuses));
        } catch (Exception e) {
            log.error("Rank query failed: state={} city={}", state, city, e);
            return ResponseEntity.internalServerError()
                    .body(Map.of("error", rootCauseMessage(e)));
        }
    }

    /**
     * RECOMPUTE trigger -- run manually (by Thale, after a listings or
     * property_values refresh), not by an investor clicking a button.
     * SYNCHRONOUS -- blocks until every matching listing is processed and
     * persisted to gap_results, then returns a summary. No job id, nothing
     * to poll: recompute is a background admin action now, not something a
     * live user waits on, so a couple minutes of blocking is fine. GET
     * /rank doesn't reflect the update until this call returns.
     *
     * since is optional (YYYY-MM-DD) -- when present, restricts the run to
     * listings with fetched_at >= since, e.g. to pick up only what a
     * targeted incremental scrape just touched instead of the whole state.
     */
    @PostMapping("/recompute")
    public ResponseEntity<?> recompute(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE) LocalDate since) {

        List<Listing> listings = listingsService.findListings(state, city, zipCode, propertyType, since);
        log.info("Recompute request: state={} city={} zipCode={} propertyType={} since={} -> {} listings",
                state, city, zipCode, propertyType, since, listings.size());
        try {
            GapRecomputeService.RecomputeSummary summary = recomputeService.recomputeAndStore(listings);
            log.info("Recompute complete: {} listings, {} computed, {} with comps",
                    summary.totalListings, summary.computed, summary.hasComps);
            return ResponseEntity.ok(summary);
        } catch (Exception e) {
            // Unlike the old job-based version, there's no job status
            // endpoint to surface this on afterward -- put the real message
            // straight in the response so curl shows it, not just Spring's
            // generic {"status":500,"error":"Internal Server Error"} body.
            // Full stack trace still goes to the server log either way.
            log.error("Recompute failed: state={} city={}", state, city, e);
            return ResponseEntity.internalServerError()
                    .body(Map.of("error", rootCauseMessage(e)));
        }
    }

    private static String rootCauseMessage(Throwable e) {
        Throwable cause = e;
        while (cause.getCause() != null && cause.getCause() != cause) {
            cause = cause.getCause();
        }
        return cause.getMessage() != null ? cause.getMessage() : cause.getClass().getSimpleName();
    }
}