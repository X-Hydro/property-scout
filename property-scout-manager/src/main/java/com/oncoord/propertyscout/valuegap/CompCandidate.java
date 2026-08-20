package com.oncoord.propertyscout.valuegap;

import com.fasterxml.jackson.databind.JsonNode;
import com.oncoord.propertyscout.model.PropertyType;

import java.util.Set;

public class CompCandidate {

    private final String propertyId;
    private final String address;
    private final String city;
    private final PropertyType propertyType;
    private final Double assessedValue;
    private final Double acres;
    private final double distanceMeters;
    private final double latitude;
    private final double longitude;
    private final Set<String> foundVia;
    private final Boolean compEligible;
    private final JsonNode geometry;

    public CompCandidate(
            String propertyId, String address, String city, PropertyType propertyType,
            Double assessedValue, Double acres, double distanceMeters,
            double latitude, double longitude, Set<String> foundVia, Boolean compEligible,
            JsonNode geometry) {
        this.propertyId = propertyId;
        this.address = address;
        this.city = city;
        this.propertyType = propertyType;
        this.assessedValue = assessedValue;
        this.acres = acres;
        this.distanceMeters = distanceMeters;
        this.latitude = latitude;
        this.longitude = longitude;
        this.foundVia = foundVia;
        this.compEligible = compEligible;
        this.geometry = geometry;
    }

    public String getPropertyId() { return propertyId; }
    public String getAddress() { return address; }
    public String getCity() { return city; }
    public PropertyType getPropertyType() { return propertyType; }
    public Double getAssessedValue() { return assessedValue; }
    public Double getAcres() { return acres; }
    public double getDistanceMeters() { return distanceMeters; }
    public double getLatitude() { return latitude; }
    public double getLongitude() { return longitude; }
    public Set<String> getFoundVia() { return foundVia; }
    public Boolean getCompEligible() { return compEligible; }
    public JsonNode getGeometry() { return geometry; }
}