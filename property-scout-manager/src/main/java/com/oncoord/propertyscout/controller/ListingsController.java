package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.Listing;
import com.oncoord.propertyscout.model.StateCityRec;
import com.oncoord.propertyscout.service.ListingsService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/listings")
public class ListingsController {

    private final ListingsService listingsService;

    public ListingsController(ListingsService listingsService) {
        this.listingsService = listingsService;
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
    
    @GetMapping("/state-city")
    public List<StateCityRec> getStateCity() {
        return listingsService.getStateCity();
    }

}