package com.oncoord.propertyscout.listingdata;

import com.oncoord.propertyscout.model.Listing;

import java.util.List;
import java.util.Set;

/**
 * Portable contract for "get me active listings" -- deliberately doesn't
 * mention RentCast, endpoints, or RentCast's own param names anywhere.
 * RentCastListingDataProvider is the only implementation today; swapping
 * providers means writing a new implementation of this interface (backed
 * by its own *Client + *Service pair, same pattern as RentCastClient/
 * RentCastService), not touching any caller.
 *
 * Returns the existing Listing model, not a separate DTO -- Listing is
 * already provider-neutral (it's shaped like the `listings` table, not
 * like any one provider's response), so a second implementation just
 * populates the same fields from its own data. `fetchedAt` is left unset
 * here; whatever does the upsert into `listings` is responsible for
 * stamping that at persistence time.
 *
 * Deliberately narrow for now -- just the one method the ingestion
 * pipeline actually needs. Add methods here (e.g. fetchRecentSales,
 * fetchAvmValue) only once there's a real caller for them; a wide
 * interface with unused methods is harder for a second provider to
 * implement faithfully than a narrow one grown as needed.
 */
public interface ListingDataProvider {

    /** Short tag identifying this provider, e.g. "rentcast" -- used as the `source` on ingested rows. */
    String getSourceName();

    List<Listing> fetchActiveListings(String state, String city, Set<String> propertyTypes);
}