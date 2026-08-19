package com.oncoord.propertyscout.model;

import java.time.LocalDate;
import java.time.OffsetDateTime;

public class PropertyValue {

    private String propertyId;
    private String state;
    private String county;
    private String municipality;
    private String parcelId;
    private String address;
    private String city;
    private String zip;

    private Double latitude;
    private Double longitude;
    private Double acreage;

    private Double assessedValue;
    private Double assessedLandValue;
    private Double assessedBuildingValue;
    private Integer assessmentYear;

    private Double lastSalePrice;
    private LocalDate lastSaleDate;

    private Double buildingSqft;
    private Double bedrooms;
    private Double bathrooms;
    private Integer yearBuilt;

    private String propertyType;

    private String source;
    private String sourceUrl;
    private LocalDate sourceDate;

    private OffsetDateTime loadedAt;

    public String getPropertyId() {
        return propertyId;
    }

    public void setPropertyId(String propertyId) {
        this.propertyId = propertyId;
    }

    public String getState() {
        return state;
    }

    public void setState(String state) {
        this.state = state;
    }

    public String getCounty() {
        return county;
    }

    public void setCounty(String county) {
        this.county = county;
    }

    public String getMunicipality() {
        return municipality;
    }

    public void setMunicipality(String municipality) {
        this.municipality = municipality;
    }

    public String getParcelId() {
        return parcelId;
    }

    public void setParcelId(String parcelId) {
        this.parcelId = parcelId;
    }

    public String getAddress() {
        return address;
    }

    public void setAddress(String address) {
        this.address = address;
    }

    public String getCity() {
        return city;
    }

    public void setCity(String city) {
        this.city = city;
    }

    public String getZip() {
        return zip;
    }

    public void setZip(String zip) {
        this.zip = zip;
    }

    public Double getLatitude() {
        return latitude;
    }

    public void setLatitude(Double latitude) {
        this.latitude = latitude;
    }

    public Double getLongitude() {
        return longitude;
    }

    public void setLongitude(Double longitude) {
        this.longitude = longitude;
    }

    public Double getAcreage() {
        return acreage;
    }

    public void setAcreage(Double acreage) {
        this.acreage = acreage;
    }

    public Double getAssessedValue() {
        return assessedValue;
    }

    public void setAssessedValue(Double assessedValue) {
        this.assessedValue = assessedValue;
    }

    public Double getAssessedLandValue() {
        return assessedLandValue;
    }

    public void setAssessedLandValue(Double assessedLandValue) {
        this.assessedLandValue = assessedLandValue;
    }

    public Double getAssessedBuildingValue() {
        return assessedBuildingValue;
    }

    public void setAssessedBuildingValue(Double assessedBuildingValue) {
        this.assessedBuildingValue = assessedBuildingValue;
    }

    public Integer getAssessmentYear() {
        return assessmentYear;
    }

    public void setAssessmentYear(Integer assessmentYear) {
        this.assessmentYear = assessmentYear;
    }

    public Double getLastSalePrice() {
        return lastSalePrice;
    }

    public void setLastSalePrice(Double lastSalePrice) {
        this.lastSalePrice = lastSalePrice;
    }

    public LocalDate getLastSaleDate() {
        return lastSaleDate;
    }

    public void setLastSaleDate(LocalDate lastSaleDate) {
        this.lastSaleDate = lastSaleDate;
    }

    public Double getBuildingSqft() {
        return buildingSqft;
    }

    public void setBuildingSqft(Double buildingSqft) {
        this.buildingSqft = buildingSqft;
    }

    public Double getBedrooms() {
        return bedrooms;
    }

    public void setBedrooms(Double bedrooms) {
        this.bedrooms = bedrooms;
    }

    public Double getBathrooms() {
        return bathrooms;
    }

    public void setBathrooms(Double bathrooms) {
        this.bathrooms = bathrooms;
    }

    public Integer getYearBuilt() {
        return yearBuilt;
    }

    public void setYearBuilt(Integer yearBuilt) {
        this.yearBuilt = yearBuilt;
    }

    public String getPropertyType() {
        return propertyType;
    }

    public void setPropertyType(String propertyType) {
        this.propertyType = propertyType;
    }

    public String getSource() {
        return source;
    }

    public void setSource(String source) {
        this.source = source;
    }

    public String getSourceUrl() {
        return sourceUrl;
    }

    public void setSourceUrl(String sourceUrl) {
        this.sourceUrl = sourceUrl;
    }

    public LocalDate getSourceDate() {
        return sourceDate;
    }

    public void setSourceDate(LocalDate sourceDate) {
        this.sourceDate = sourceDate;
    }

    public OffsetDateTime getLoadedAt() {
        return loadedAt;
    }

    public void setLoadedAt(OffsetDateTime loadedAt) {
        this.loadedAt = loadedAt;
    }
}