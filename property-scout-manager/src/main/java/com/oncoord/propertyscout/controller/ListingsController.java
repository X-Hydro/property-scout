package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.Listing;
import com.oncoord.propertyscout.model.StateCityRec;
import com.oncoord.propertyscout.service.ListingIngestionService;
import com.oncoord.propertyscout.service.ListingsService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;
import java.util.Set;

@RestController
@RequestMapping("/api/listings")
public class ListingsController {

    private final ListingsService listingsService;
    private final ListingIngestionService listingIngestionService;

    public ListingsController(ListingsService listingsService, ListingIngestionService listingIngestionService) {
        this.listingsService = listingsService;
        this.listingIngestionService = listingIngestionService;
    }

    @GetMapping
    public ResponseEntity<List<Listing>> getListings(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType) {

        return ResponseEntity.ok(
                listingsService.findListings(
                        state,
                        city,
                        zipCode,
                        propertyType
                )
        );
    }

    @GetMapping("/{listingId}")
    public ResponseEntity<Listing> getListing(@PathVariable String listingId) {
        return listingsService.findById(listingId)
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    /**
     * Triggers a live pull from the active ListingDataProvider (paid,
     * cached at the provider layer) and upserts the results into
     * `listings`. propertyTypes, if given, must be in the PROVIDER's own
     * vocabulary (RentCast: "Single Family", "Land") -- see
     * ListingIngestionService's javadoc for why this isn't the
     * PropertyType enum used by property_values.
     */
    @PostMapping("/refresh")
    public Map<String, Object> refreshListings(
            @RequestParam String state,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) Set<String> propertyTypes) {

        int count = listingIngestionService.ingestActiveListings(state, city, propertyTypes);
        return Map.of("upserted", count);
    }

    @GetMapping("/state-city")
    public List<StateCityRec> getStateCity() {
        return listingsService.getStateCity();
    }

}