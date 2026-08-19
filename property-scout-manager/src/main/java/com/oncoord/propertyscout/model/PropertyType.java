package com.oncoord.propertyscout.model;

public enum PropertyType {

    SINGLE_FAMILY("Single Family"),
    MULTI_FAMILY("Multi Family"),
    TWO_FAMILY("Two Family"),
    VACANT_LAND("Vacant Land");

    private final String value;

    PropertyType(String value) {
        this.value = value;
    }

    public String getValue() {
        return value;
    }

    @Override
    public String toString() {
        return value;
    }

    /**
     * Lenient lookup by value (case-insensitive), for parsing raw text out
     * of property_values.property_type -- scraped assessor data, so it can
     * contain values this enum doesn't cover (Commercial, Condo - No Land,
     * Common Land, etc). Returns null rather than throwing on those; callers
     * treat null as "not a known-good comp type", not "unknown/unverified" --
     * the distinction between "no value at all" and "a value that just isn't
     * PropertyType" still needs to be made by the caller from the raw string.
     */
    public static PropertyType fromValue(String value) {
        if (value == null) {
            return null;
        }
        for (PropertyType type : values()) {
            if (type.value.equalsIgnoreCase(value.trim())) {
                return type;
            }
        }
        return null;
    }
}