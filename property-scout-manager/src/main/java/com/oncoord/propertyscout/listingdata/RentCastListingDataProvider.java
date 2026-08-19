package com.oncoord.propertyscout.listingdata;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.propertyscout.model.Listing;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RentCast's implementation of ListingDataProvider. Everything RentCast-
 * specific -- the endpoint path, RentCast's own query param names, and its
 * JSON response shape -- is contained here. Callers only see
 * ListingDataProvider's domain-shaped method and the existing Listing model.
 * Lives in the same neutral `listingdata` package as the interface
 * (deliberately no `rentcast` package) so nothing about the package
 * structure implies lock-in to one provider -- a second implementation
 * would sit right next to this one, same package, different class name.
 *
 * Endpoint, params, and header confirmed against a working curl call
 * (get_listings.sh): GET https://api.rentcast.io/v1/listings/sale with
 * city/state/status/propertyType/limit/includeTotalCount and an X-Api-Key
 * header. propertyType takes multiple values pipe-delimited, e.g.
 * "Single Family|Land" -- sent server-side so RentCast only returns what's
 * needed, since every listing it doesn't need to return is money saved.
 */
@Component
public class RentCastListingDataProvider implements ListingDataProvider {

    private static final String LISTINGS_ENDPOINT = "listings/sale";
    private static final Duration LISTINGS_TTL = Duration.ofHours(6); // active listings go stale fast
    private static final int DEFAULT_LIMIT = 50; // matches get_listings.sh; RentCast may support a higher max -- confirm before raising

    private final RentCastService rentCastService;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public RentCastListingDataProvider(RentCastService rentCastService) {
        this.rentCastService = rentCastService;
    }

    @Override
    public String getSourceName() {
        return "rentcast";
    }

    @Override
    public List<Listing> fetchActiveListings(String state, String city, Set<String> propertyTypes) {
        Map<String, String> params = new HashMap<>();
        params.put("state", state);
        if (city != null && !city.isBlank()) {
            params.put("city", city);
        }
        params.put("status", "Active");
        if (propertyTypes != null && !propertyTypes.isEmpty()) {
            params.put("propertyType", String.join("|", propertyTypes));
        }
        params.put("limit", String.valueOf(DEFAULT_LIMIT));
        params.put("includeTotalCount", "true");

        RentCastService.ListingSourceResult result = rentCastService.fetch(LISTINGS_ENDPOINT, params, LISTINGS_TTL);

        List<Listing> listings = new ArrayList<>();
        try {
            JsonNode root = objectMapper.readTree(result.body());
            // includeTotalCount=true may wrap the array in an object (e.g.
            // {"listings": [...], "total": N}) rather than returning a bare
            // array -- confirm the real shape against a live response and
            // adjust this if root isn't a plain array.
            if (!root.isArray()) {
                return listings;
            }
            for (JsonNode node : root) {
                listings.add(toListing(node));
            }
        } catch (Exception e) {
            throw new IllegalStateException("Failed to parse RentCast listings response", e);
        }
        return listings;
    }

    private Listing toListing(JsonNode node) {
        Listing l = new Listing();
        l.setListingId(text(node, "id"));
        l.setFormattedAddress(text(node, "formattedAddress"));
        l.setAddressLine1(text(node, "addressLine1"));
        l.setAddressLine2(text(node, "addressLine2"));
        l.setCity(text(node, "city"));
        l.setState(text(node, "state"));
        l.setZipCode(text(node, "zipCode"));
        l.setCounty(text(node, "county"));
        l.setLatitude(decimal(node, "latitude"));
        l.setLongitude(decimal(node, "longitude"));
        l.setPropertyType(text(node, "propertyType"));
        l.setBedrooms(decimal(node, "bedrooms"));
        l.setBathrooms(decimal(node, "bathrooms"));
        l.setSquareFootage(decimal(node, "squareFootage"));
        l.setLotSize(decimal(node, "lotSize"));
        l.setYearBuilt(integer(node, "yearBuilt"));
        l.setStatus(text(node, "status"));
        l.setPrice(decimal(node, "price"));
        l.setListingType(text(node, "listingType"));
        l.setListedDate(date(node, "listedDate"));
        l.setRemovedDate(date(node, "removedDate"));
        l.setDaysOnMarket(integer(node, "daysOnMarket"));
        l.setMlsName(text(node, "mlsName"));
        l.setMlsNumber(text(node, "mlsNumber"));
        l.setAgent(node.get("listingAgent"));
        l.setOffice(node.get("listingOffice"));
        l.setPriceHistory(node.get("history"));
        l.setSource(getSourceName());
        // fetchedAt intentionally left unset -- stamped at persistence time
        // by whatever upserts this into the `listings` table.
        return l;
    }

    private String text(JsonNode node, String field) {
        JsonNode v = node.get(field);
        return (v == null || v.isNull()) ? null : v.asText();
    }

    private Double decimal(JsonNode node, String field) {
        JsonNode v = node.get(field);
        return (v == null || v.isNull()) ? null : v.asDouble();
    }

    private Integer integer(JsonNode node, String field) {
        JsonNode v = node.get(field);
        return (v == null || v.isNull()) ? null : v.asInt();
    }

    private LocalDate date(JsonNode node, String field) {
        String s = text(node, field);
        if (s == null) {
            return null;
        }
        return OffsetDateTime.parse(s).toLocalDate();
    }
}
