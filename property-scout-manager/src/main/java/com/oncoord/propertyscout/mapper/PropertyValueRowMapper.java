package com.oncoord.propertyscout.mapper;

import com.oncoord.propertyscout.model.PropertyValue;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Component;

import java.sql.ResultSet;
import java.sql.SQLException;

@Component
public class PropertyValueRowMapper implements RowMapper<PropertyValue> {

    @Override
    public PropertyValue mapRow(ResultSet rs, int rowNum) throws SQLException {
        PropertyValue propertyValue = new PropertyValue();

        propertyValue.setPropertyId(rs.getString("property_id"));
        propertyValue.setState(rs.getString("state"));
        propertyValue.setCounty(rs.getString("county"));
        propertyValue.setMunicipality(rs.getString("municipality"));
        propertyValue.setParcelId(rs.getString("parcel_id"));
        propertyValue.setAddress(rs.getString("address"));
        propertyValue.setCity(rs.getString("city"));
        propertyValue.setZip(rs.getString("zip"));

        propertyValue.setLatitude(
                rs.getObject("latitude", Double.class)
        );

        propertyValue.setLongitude(
                rs.getObject("longitude", Double.class)
        );

        propertyValue.setAcreage(
                rs.getObject("acreage", Double.class)
        );

        propertyValue.setAssessedValue(
                rs.getObject("assessed_value", Double.class)
        );

        propertyValue.setAssessedLandValue(
                rs.getObject("assessed_land_value", Double.class)
        );

        propertyValue.setAssessedBuildingValue(
                rs.getObject("assessed_building_value", Double.class)
        );

        propertyValue.setAssessmentYear(
                rs.getObject("assessment_year", Integer.class)
        );

        propertyValue.setLastSalePrice(
                rs.getObject("last_sale_price", Double.class)
        );

        propertyValue.setLastSaleDate(
                rs.getObject("last_sale_date", java.time.LocalDate.class)
        );

        propertyValue.setBuildingSqft(
                rs.getObject("building_sqft", Double.class)
        );

        propertyValue.setBedrooms(
                rs.getObject("bedrooms", Double.class)
        );

        propertyValue.setBathrooms(
                rs.getObject("bathrooms", Double.class)
        );

        propertyValue.setYearBuilt(
                rs.getObject("year_built", Integer.class)
        );

        propertyValue.setPropertyType(
                rs.getString("property_type")
        );

        propertyValue.setSource(
                rs.getString("source")
        );

        propertyValue.setSourceUrl(
                rs.getString("source_url")
        );

        propertyValue.setSourceDate(
                rs.getObject("source_date", java.time.LocalDate.class)
        );

        propertyValue.setLoadedAt(
                rs.getObject("loaded_at", java.time.OffsetDateTime.class)
        );

        return propertyValue;
    }
}