package com.oncoord.propertyscout.controller;

import com.oncoord.propertyscout.model.PropertyValue;
import com.oncoord.propertyscout.service.PropertyValuesService;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/property-values")
public class PropertyValuesController {

    private final PropertyValuesService propertyValuesService;

    public PropertyValuesController(PropertyValuesService propertyValuesService) {
        this.propertyValuesService = propertyValuesService;
    }

    @GetMapping
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
}