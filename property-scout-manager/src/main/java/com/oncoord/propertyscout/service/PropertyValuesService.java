package com.oncoord.propertyscout.service;

import com.oncoord.propertyscout.mapper.PropertyValueRowMapper;
import com.oncoord.propertyscout.model.PropertyType;
import com.oncoord.propertyscout.model.PropertyValue;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

@Service
public class PropertyValuesService {

    private static final double DEFAULT_RADIUS_METERS = 300.0;
    private static final double MAX_RADIUS_METERS = 5000.0;

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

    /**
     * Radius lookup around a point, e.g. to reproduce compute_gap.py's
     * "nearby comps" step server-side instead of re-running the Python
     * pipeline. Distance is computed by casting the existing `geometry`
     * column to `geography` (so ST_DWithin/ST_Distance are in meters, not
     * degrees) against the GIST index on that column.
     *
     * @param latitude       target latitude (WGS84)
     * @param longitude      target longitude (WGS84)
     * @param radiusMeters   search radius in meters; null falls back to
     *                       DEFAULT_RADIUS_METERS, and anything over
     *                       MAX_RADIUS_METERS is clamped to avoid an
     *                       accidental table-wide scan
     * @param propertyTypes   optional exact-match filter, e.g. "Single Family"
     */
    public List<PropertyValue> findNearby(
            double latitude,
            double longitude,
            Double radiusMeters,
            List<PropertyType> propertyTypes) {

        double radius = radiusMeters == null
                ? DEFAULT_RADIUS_METERS
                : radiusMeters;

        if (radius <= 0) {
            radius = DEFAULT_RADIUS_METERS;
        }

        if (radius > MAX_RADIUS_METERS) {
            radius = MAX_RADIUS_METERS;
        }

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
            loaded_at,
            ST_Distance(
                geometry::geography,
                ST_SetSRID(ST_MakePoint(?, ?), 4326)::geography
            ) AS distance_meters
        FROM property_values
        WHERE geometry IS NOT NULL
          AND ST_DWithin(
                geometry::geography,
                ST_SetSRID(ST_MakePoint(?, ?), 4326)::geography,
                ?
          )
        """);

        // ST_MakePoint takes (longitude, latitude)
        List<Object> parameters = new ArrayList<>();

        parameters.add(longitude);
        parameters.add(latitude);

        parameters.add(longitude);
        parameters.add(latitude);

        parameters.add(radius);

        if (propertyTypes != null && !propertyTypes.isEmpty()) {
            sql.append(" AND property_type IN (");
            for (int i = 0; i < propertyTypes.size(); i++) {
                if (i > 0) {
                    sql.append(", ");
                }
                sql.append("?");
                parameters.add(propertyTypes.get(i).getValue());
            }

            sql.append(")");
        }

        sql.append(" ORDER BY distance_meters ASC");

        return jdbcTemplate.query(
                sql.toString(),
                propertyValueRowMapper,
                parameters.toArray()
        );
    }
}