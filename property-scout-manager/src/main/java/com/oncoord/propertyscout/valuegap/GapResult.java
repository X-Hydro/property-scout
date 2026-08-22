package com.oncoord.propertyscout.valuegap;

import java.util.List;

public class GapResult {

    private final String listingId;
    private final String address;
    private final String propertyType;
    private final Integer yearBuilt;
    private final double price;
    private final Double targetAssessedValue;
    private final boolean targetIsLand;
    private final List<CompCandidate> candidates;

    private final boolean hasComps;
    private final List<CompCandidate> comps;
    private final Double compMedian;
    private final Double compMin;
    private final Double compMax;
    private final Double gap;
    private final Double gapPct;

    /** Set later by GapRankingService once the full ranked group is known. */
    private Double relativeGapPct;

    private final Double targetLatitude;
    private final Double targetLongitude;
    private final com.fasterxml.jackson.databind.JsonNode targetGeometry;

    public GapResult(
            String listingId, String address, String propertyType, Integer yearBuilt,
            double price, Double targetAssessedValue,
            Double targetLatitude, Double targetLongitude, com.fasterxml.jackson.databind.JsonNode targetGeometry,
            boolean targetIsLand,
            List<CompCandidate> candidates, boolean hasComps, List<CompCandidate> comps,
            Double compMedian, Double compMin, Double compMax, Double gap, Double gapPct) {
        this.listingId = listingId;
        this.address = address;
        this.propertyType = propertyType;
        this.yearBuilt = yearBuilt;
        this.price = price;
        this.targetAssessedValue = targetAssessedValue;
        this.targetLatitude = targetLatitude;
        this.targetLongitude = targetLongitude;
        this.targetGeometry = targetGeometry;
        this.targetIsLand = targetIsLand;
        this.candidates = candidates;
        this.hasComps = hasComps;
        this.comps = comps;
        this.compMedian = compMedian;
        this.compMin = compMin;
        this.compMax = compMax;
        this.gap = gap;
        this.gapPct = gapPct;
    }

    public Double getTargetLatitude() { return targetLatitude; }
    public Double getTargetLongitude() { return targetLongitude; }
    public com.fasterxml.jackson.databind.JsonNode getTargetGeometry() { return targetGeometry; }

    public String getListingId() {
        return listingId;
    }

    public String getAddress() {
        return address;
    }

    public String getPropertyType() {
        return propertyType;
    }

    public Integer getYearBuilt() {
        return yearBuilt;
    }

    public double getPrice() {
        return price;
    }

    public Double getTargetAssessedValue() {
        return targetAssessedValue;
    }

    public boolean isTargetIsLand() {
        return targetIsLand;
    }

    public List<CompCandidate> getCandidates() {
        return candidates;
    }

    public boolean isHasComps() {
        return hasComps;
    }

    public List<CompCandidate> getComps() {
        return comps;
    }

    public Double getCompMedian() {
        return compMedian;
    }

    public Double getCompMin() {
        return compMin;
    }

    public Double getCompMax() {
        return compMax;
    }

    public Double getGap() {
        return gap;
    }

    public Double getGapPct() {
        return gapPct;
    }

    public Double getRelativeGapPct() {
        return relativeGapPct;
    }

    public void setRelativeGapPct(Double relativeGapPct) {
        this.relativeGapPct = relativeGapPct;
    }
}