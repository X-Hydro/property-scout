package com.oncoord.propertyscout.controller;


import com.oncoord.propertyscout.model.Listing;
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
}