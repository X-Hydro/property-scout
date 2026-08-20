package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.PropertyType;

public class TargetParcel {

    private final String propertyId;
    private final String geometryWkt;
    private final String geometryGeoJson;
    private final String address;
    private final Double acres;
    private final Double assessedValue;
    private final PropertyType propertyType;

    public TargetParcel(
            String propertyId, String geometryWkt, String geometryGeoJson, String address,
            Double acres, Double assessedValue, PropertyType propertyType) {
        this.propertyId = propertyId;
        this.geometryWkt = geometryWkt;
        this.geometryGeoJson = geometryGeoJson;
        this.address = address;
        this.acres = acres;
        this.assessedValue = assessedValue;
        this.propertyType = propertyType;
    }

    public String getPropertyId() {
        return propertyId;
    }

    public String getGeometryWkt() {
        return geometryWkt;
    }

    public String getGeometryGeoJson() {
        return geometryGeoJson;
    }

    public String getAddress() {
        return address;
    }

    public Double getAcres() {
        return acres;
    }

    public Double getAssessedValue() {
        return assessedValue;
    }

    public PropertyType getPropertyType() {
        return propertyType;
    }
}