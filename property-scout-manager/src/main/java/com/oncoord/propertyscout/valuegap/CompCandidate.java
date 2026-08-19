
package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.PropertyType;

import java.util.Set;

/**
 * One candidate neighbor parcel for a target listing, with the reason(s) it
 * was found and whether it's usable as a value comp. Mirrors a single
 * "neighbor_parcel" feature's properties from find_abutters.py's output.
 */
public class CompCandidate {

    private final String propertyId;
    private final String address;
    private final String city;
    /** VGSI land-use type, parsed from property_values.property_type. Null = no assessor match or an unrecognized type. */
    private final PropertyType propertyType;
    private final Double assessedValue;
    private final Double acres;
    private final double distanceMeters;
    private final double latitude;
    private final double longitude;
    /** "geometric", "near_similar_size", "near_same_street" — can hold more than one. */
    private final Set<String> foundVia;
    /** true = known comparable type, null = unverified (no assessor match), never false (false is excluded, not tagged). */
    private final Boolean compEligible;

    public CompCandidate(
            String propertyId, String address, String city, PropertyType propertyType,
            Double assessedValue, Double acres, double distanceMeters,
            double latitude, double longitude, Set<String> foundVia, Boolean compEligible) {
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
    }

    public String getPropertyId() {
        return propertyId;
    }

    public String getAddress() {
        return address;
    }

    public String getCity() {
        return city;
    }

    public PropertyType getPropertyType() {
        return propertyType;
    }

    public Double getAssessedValue() {
        return assessedValue;
    }

    public Double getAcres() {
        return acres;
    }

    public double getDistanceMeters() {
        return distanceMeters;
    }

    public double getLatitude() {
        return latitude;
    }

    public double getLongitude() {
        return longitude;
    }

    public Set<String> getFoundVia() {
        return foundVia;
    }

    public Boolean getCompEligible() {
        return compEligible;
    }
}