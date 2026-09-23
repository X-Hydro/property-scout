package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.PropertyType;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * Part 2 of the ValueGap pipeline: given a target listing and its candidate
 * comps (from NearbyCompsService), compute the median-based gap. Direct
 * port of compute_gap.py's compute_gap_for_group().
 *
 * Two different vocabularies are in play here, deliberately not unified:
 * `propertyType` (the target listing's own type, e.g. RentCast's "Land"/
 * "Single Family"/"Condo") stays a raw String -- it's RentCast's listing
 * category, not PropertyType (VGSI assessor land-use). Each comp's type,
 * on the other hand, comes from property_values via NearbyCompsService and
 * is already a PropertyType.
 */
@Service
public class GapComputationService {

    // Narrower than NearbyCompsService.COMP_ELIGIBLE_TYPES on purpose: land
    // and finished homes are different value classes, so only built homes
    // count toward the median a listing (house or land) is measured against.
    private static final PropertyType VALUE_COMP_TYPE = PropertyType.SINGLE_FAMILY;

    // Cap on how many eligible comps feed the median. Once a neighborhood
    // has more than this many SINGLE_FAMILY-eligible candidates, the
    // farthest ones are dropped from the VALUE calculation (median/min/max/
    // gap) -- distant comps in a dense area can otherwise pull the median
    // toward a different part of the neighborhood than the target actually
    // sits in. This does NOT affect `candidates` (the full found-nearby
    // list still shown on the map) or CompCandidate.compEligible -- only
    // which of the eligible comps actually participate in the value math.
    private static final int MAX_VALUE_COMPS = 15;

    public GapResult compute(
            String listingId,
            String address,
            String propertyType,
            Integer yearBuilt,
            double price,
            Double targetAssessedValue,
            Double targetLatitude,
            Double targetLongitude,
            com.fasterxml.jackson.databind.JsonNode targetGeometry,
            List<CompCandidate> candidates,
            String listingUrl) {

        boolean targetIsLand = "Land".equals(propertyType);
        List<CompCandidate> comps = new ArrayList<>();
        for (CompCandidate c : candidates) {
            if (c.getPropertyType() == VALUE_COMP_TYPE && c.getAssessedValue() != null) {
                comps.add(c);
            }
        }

        if (comps.size() > MAX_VALUE_COMPS) {
            comps.sort(Comparator.comparingDouble(CompCandidate::getDistanceMeters));
            comps = new ArrayList<>(comps.subList(0, MAX_VALUE_COMPS));
        }

        if (comps.isEmpty()) {
            return new GapResult(
                    listingId, address, propertyType, yearBuilt, price, targetAssessedValue,
                    targetLatitude, targetLongitude, targetGeometry,
                    targetIsLand, candidates, false, comps, null, null, null, null, null,
                    listingUrl
            );
        }

        List<Double> values = new ArrayList<>();
        for (CompCandidate c : comps) {
            values.add(c.getAssessedValue());
        }
        Collections.sort(values);
        int n = values.size();
        double median = (n % 2 == 1)
                ? values.get(n / 2)
                : (values.get(n / 2 - 1) + values.get(n / 2)) / 2.0;
        double min = values.get(0);
        double max = values.get(n - 1);
        double gap = median - price;
        double gapPct = (gap / price) * 100.0;

        return new GapResult(
                listingId, address, propertyType, yearBuilt, price, targetAssessedValue,
                targetLatitude, targetLongitude, targetGeometry,
                targetIsLand, candidates, true, comps, median, min, max, gap, gapPct,
                listingUrl
        );

    }
}