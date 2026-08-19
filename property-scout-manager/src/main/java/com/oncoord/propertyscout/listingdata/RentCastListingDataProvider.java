package com.oncoord.propertyscout.listingdata;


import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.propertyscout.listingdata.ListingDataProvider;
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
 *
 * NOTE: the endpoint path and param names below (LISTINGS_ENDPOINT,
 * "status"/"propertyType"/"limit") are best-guess based on RentCast's
 * general API conventions -- confirm the exact path/params/pagination
 * against RentCast's own docs before relying on this against live traffic.
 */
@Component
public class RentCastListingDataProvider implements ListingDataProvider {

    private static final String LISTINGS_ENDPOINT = "listings/sale";
    private static final Duration LISTINGS_TTL = Duration.ofHours(6); // active listings go stale fast

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

        RentCastService.ListingSourceResult result = rentCastService.fetch(LISTINGS_ENDPOINT, params, LISTINGS_TTL);

        List<Listing> listings = new ArrayList<>();
        try {
            JsonNode root = objectMapper.readTree(result.body());
            if (!root.isArray()) {
                return listings;
            }
            for (JsonNode node : root) {
                String type = text(node, "propertyType");
                if (propertyTypes != null && !propertyTypes.isEmpty() && !propertyTypes.contains(type)) {
                    continue;
                }
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
