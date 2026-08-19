package com.oncoord.propertyscout.service;

import com.oncoord.propertyscout.mapper.PropertyValueRowMapper;
import com.oncoord.propertyscout.model.PropertyValue;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

@Service
public class PropertyValuesService {

    private final JdbcTemplate jdbcTemplate;
    private final PropertyValueRowMapper propertyValueRowMapper;

    public PropertyValuesService(
            JdbcTemplate jdbcTemplate,
            PropertyValueRowMapper propertyValueRowMapper) {

        this.jdbcTemplate = jdbcTemplate;
        this.propertyValueRowMapper = propertyValueRowMapper;
    }

    public List<PropertyValue> findPropertyValues(
            String state,
            String municipality,
            String city,
            String zipCode,
            String propertyType) {

        StringBuilder sql = new StringBuilder("""
            SELECT
                property_id,
                state,
                county,
                municipality,
                parcel_id,
                address,
                city,
                zip,
                latitude,
                longitude,
                acreage,
                assessed_value,
                assessed_land_value,
                assessed_building_value,
                assessment_year,
                last_sale_price,
                last_sale_date,
                building_sqft,
                bedrooms,
                bathrooms,
                year_built,
                property_type,
                source,
                source_url,
                source_date,
                loaded_at
            FROM property_values
            WHERE UPPER(state) = UPPER(?)
            """);

        List<Object> parameters = new ArrayList<>();
        parameters.add(state);

        if (municipality != null && !municipality.isBlank()) {
            sql.append(" AND UPPER(municipality) = UPPER(?)");
            parameters.add(municipality);
        }

        if (city != null && !city.isBlank()) {
            sql.append(" AND UPPER(city) = UPPER(?)");
            parameters.add(city);
        }

        if (zipCode != null && !zipCode.isBlank()) {
            sql.append(" AND zip = ?");
            parameters.add(zipCode);
        }

        if (propertyType != null && !propertyType.isBlank()) {
            sql.append(" AND UPPER(property_type) = UPPER(?)");
            parameters.add(propertyType);
        }

        sql.append(" ORDER BY property_id");

        return jdbcTemplate.query(
                sql.toString(),
                propertyValueRowMapper,
                parameters.toArray()
        );
    }
}