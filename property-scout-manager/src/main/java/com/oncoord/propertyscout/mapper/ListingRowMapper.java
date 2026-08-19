package com.oncoord.propertyscout.mapper;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.oncoord.propertyscout.model.Listing;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Component;

import java.sql.ResultSet;
import java.sql.SQLException;

@Component
public class ListingRowMapper implements RowMapper<Listing> {

    private final ObjectMapper objectMapper;

    public ListingRowMapper(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @Override
    public Listing mapRow(ResultSet rs, int rowNum) throws SQLException {
        Listing listing = new Listing();

        listing.setListingId(rs.getString("listing_id"));
        listing.setFormattedAddress(rs.getString("formatted_address"));
        listing.setAddressLine1(rs.getString("address_line_1"));
        listing.setAddressLine2(rs.getString("address_line_2"));
        listing.setCity(rs.getString("city"));
        listing.setState(rs.getString("state"));
        listing.setZipCode(rs.getString("zip_code"));
        listing.setCounty(rs.getString("county"));

        listing.setLatitude(rs.getObject("latitude", Double.class));
        listing.setLongitude(rs.getObject("longitude", Double.class));

        listing.setPropertyType(rs.getString("property_type"));
        listing.setBedrooms(rs.getObject("bedrooms", Double.class));
        listing.setBathrooms(rs.getObject("bathrooms", Double.class));
        listing.setSquareFootage(rs.getObject("square_footage", Double.class));
        listing.setLotSize(rs.getObject("lot_size", Double.class));
        listing.setYearBuilt(rs.getObject("year_built", Integer.class));

        listing.setStatus(rs.getString("status"));
        listing.setPrice(rs.getObject("price", Double.class));
        listing.setListingType(rs.getString("listing_type"));

        listing.setListedDate(
                rs.getObject("listed_date", java.time.LocalDate.class)
        );

        listing.setRemovedDate(
                rs.getObject("removed_date", java.time.LocalDate.class)
        );

        listing.setDaysOnMarket(
                rs.getObject("days_on_market", Integer.class)
        );

        listing.setMlsName(rs.getString("mls_name"));
        listing.setMlsNumber(rs.getString("mls_number"));

        listing.setAgent(readJson(rs, "agent"));
        listing.setOffice(readJson(rs, "office"));
        listing.setPriceHistory(readJson(rs, "price_history"));

        listing.setSource(rs.getString("source"));

        listing.setFetchedAt(
                rs.getObject("fetched_at", java.time.OffsetDateTime.class)
        );

        return listing;
    }

    private JsonNode readJson(ResultSet rs, String column) throws SQLException {
        String json = rs.getString(column);

        if (json == null) {
            return null;
        }

        try {
            return objectMapper.readTree(json);
        } catch (Exception e) {
            throw new SQLException(
                    "Unable to parse JSON from column: " + column, e );
        }
    }
}