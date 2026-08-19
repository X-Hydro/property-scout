package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.PropertyType;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collections;
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

    public GapResult compute(
            String listingId,
            String address,
            String propertyType,
            Integer yearBuilt,
            double price,
            Double targetAssessedValue,
            List<CompCandidate> candidates) {

        boolean targetIsLand = "Land".equals(propertyType);

        List<Double> comps = new ArrayList<>();
        for (CompCandidate c : candidates) {
            if (c.getPropertyType() == VALUE_COMP_TYPE && c.getAssessedValue() != null) {
                comps.add(c.getAssessedValue());
            }
        }

        if (comps.isEmpty()) {
            return new GapResult(
                    listingId, address, propertyType, yearBuilt, price, targetAssessedValue,
                    targetIsLand, candidates, false, comps, null, null, null, null, null
            );
        }

        Collections.sort(comps);
        int n = comps.size();
        double median = (n % 2 == 1)
                ? comps.get(n / 2)
                : (comps.get(n / 2 - 1) + comps.get(n / 2)) / 2.0;
        double min = comps.get(0);
        double max = comps.get(n - 1);
        double gap = median - price;
        double gapPct = (gap / price) * 100.0;

        return new GapResult(
                listingId, address, propertyType, yearBuilt, price, targetAssessedValue,
                targetIsLand, candidates, true, comps, median, min, max, gap, gapPct
        );
    }
}