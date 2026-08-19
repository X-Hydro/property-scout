package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.PropertyType;
import com.oncoord.propertyscout.model.PropertyValue;
import com.oncoord.propertyscout.service.PropertyValuesService;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
public class PropertyValuesController {

    private static final double DEFAULT_RADIUS_METERS = 300.0;
    private static final double MAX_RADIUS_METERS = 1000.0;

    private final PropertyValuesService propertyValuesService;

    public PropertyValuesController(PropertyValuesService propertyValuesService) {
        this.propertyValuesService = propertyValuesService;
    }

    @GetMapping("/api/property-values")
    public List<PropertyValue> getPropertyValues(
            @RequestParam String state,
            @RequestParam(required = false) String municipality,
            @RequestParam(required = false) String city,
            @RequestParam(required = false) String zipCode,
            @RequestParam(required = false) String propertyType) {

        return propertyValuesService.findPropertyValues(
                state,
                municipality,
                city,
                zipCode,
                propertyType
        );
    }

    @GetMapping("/api/nearby-property-values")
    public List<PropertyValue> getNearbyPropertyValues(
            @RequestParam double latitude,
            @RequestParam double longitude,
            @RequestParam(required = false) Double radiusMeters,
            @RequestParam(required = false) List<PropertyType> propertyTypes) {

        double radius = radiusMeters == null
                ? DEFAULT_RADIUS_METERS
                : radiusMeters;

        if (radius <= 0) {
            radius = DEFAULT_RADIUS_METERS;
        }

        if (radius > MAX_RADIUS_METERS) {
            radius = MAX_RADIUS_METERS;
        }

        if (propertyTypes == null || propertyTypes.isEmpty()) {
            propertyTypes = List.of(PropertyType.SINGLE_FAMILY, PropertyType.VACANT_LAND);
        }

        return propertyValuesService.findNearby(
                latitude,
                longitude,
                radius,
                propertyTypes
        );
    }
}