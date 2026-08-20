package com.oncoord.propertyscout.service;

import com.oncoord.propertyscout.listingdata.ListingDataProvider;
import com.oncoord.propertyscout.model.Listing;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;

/**
 * The piece that was still missing: turns a live ListingDataProvider pull
 * into rows in `listings`. Caching (listing_source_cache) already happens
 * for free inside RentCastService/whichever *Service backs the active
 * provider -- this is the separate step of persisting the parsed result.
 *
 * Depends on the ListingDataProvider interface, not RentCastListingDataProvider
 * directly, so swapping providers doesn't touch this class.
 */
@Service
public class ListingIngestionService {

    private final ListingDataProvider listingDataProvider;
    private final ListingsService listingsService;

    public ListingIngestionService(ListingDataProvider listingDataProvider, ListingsService listingsService) {
        this.listingDataProvider = listingDataProvider;
        this.listingsService = listingsService;
    }

    /**
     * @param propertyTypes filter values in the ACTIVE PROVIDER's own
     *                       vocabulary (RentCast: "Single Family", "Land",
     *                       etc.) -- NOT the assessor's land_use_desc
     *                       vocabulary used by property_values/PropertyType
     *                       ("Vacant Land"). The two don't match; passing
     *                       PropertyType.VACANT_LAND.getValue() here would
     *                       silently filter out every land listing.
     * @return number of listings upserted
     */
    public int ingestActiveListings(String state, String city, Set<String> propertyTypes) {
        List<Listing> fetched = listingDataProvider.fetchActiveListings(state, city, propertyTypes);

        OffsetDateTime now = OffsetDateTime.now();
        fetched.forEach(l -> l.setFetchedAt(now));

        return listingsService.upsertAll(fetched);
    }
}