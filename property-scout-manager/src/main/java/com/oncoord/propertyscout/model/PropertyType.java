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
}