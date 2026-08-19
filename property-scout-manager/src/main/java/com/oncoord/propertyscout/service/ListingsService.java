package com.oncoord.propertyscout.service;

import com.oncoord.propertyscout.mapper.ListingRowMapper;
import com.oncoord.propertyscout.model.Listing;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

@Service
public class ListingsService {

    private final JdbcTemplate jdbcTemplate;
    private final ListingRowMapper listingRowMapper;

    public ListingsService(
            JdbcTemplate jdbcTemplate,
            ListingRowMapper listingRowMapper) {

        this.jdbcTemplate = jdbcTemplate;
        this.listingRowMapper = listingRowMapper;
    }

    public List<Listing> findListings(
            String state,
            String city,
            String zipCode,
            String propertyType) {

        StringBuilder sql = new StringBuilder("""
            SELECT
                listing_id,
                formatted_address,
                address_line_1,
                address_line_2,
                city,
                state,
                zip_code,
                county,
                latitude,
                longitude,
                property_type,
                bedrooms,
                bathrooms,
                square_footage,
                lot_size,
                year_built,
                status,
                price,
                listing_type,
                listed_date,
                removed_date,
                days_on_market,
                mls_name,
                mls_number,
                agent,
                office,
                price_history,
                source,
                fetched_at
            FROM listings
            WHERE UPPER(state) = UPPER(?)
            """);

        List<Object> parameters = new ArrayList<>();
        parameters.add(state);

        if (city != null && !city.isBlank()) {
            sql.append(" AND UPPER(city) = UPPER(?)");
            parameters.add(city);
        }

        if (zipCode != null && !zipCode.isBlank()) {
            sql.append(" AND zip_code = ?");
            parameters.add(zipCode);
        }

        if (propertyType != null && !propertyType.isBlank()) {
            sql.append(" AND UPPER(property_type) = UPPER(?)");
            parameters.add(propertyType);
        }

        sql.append(" ORDER BY listed_date DESC NULLS LAST");

        return jdbcTemplate.query(
                sql.toString(),
                listingRowMapper,
                parameters.toArray()
        );
    }
}