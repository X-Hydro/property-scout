package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.Listing;
import com.oncoord.propertyscout.service.ListingsService;
import com.oncoord.propertyscout.valuegap.GapRankingService;
import com.oncoord.propertyscout.valuegap.GapResult;
import com.oncoord.propertyscout.valuegap.ValueGapPipelineService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/value-gap")
public class ValueGapController {

    private final ListingsService listingsService;
    private final ValueGapPipelineService pipelineService;

    public ValueGapController(ListingsService listingsService, ValueGapPipelineService pipelineService) {
        this.listingsService = listingsService;
        this.pipelineService = pipelineService;
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
     */
    @GetMapping("/rank")
    public Map<String, Object> rankGaps(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType) {

        List<Listing> listings = listingsService.findListings(state, city, zipCode, propertyType);
        GapRankingService.RankedGaps ranked = pipelineService.rankListings(listings);

        return Map.of(
                "rankedByPropertyType", ranked.getRankedByPropertyType(),
                "noComps", ranked.getNoComps()
        );
    }
}