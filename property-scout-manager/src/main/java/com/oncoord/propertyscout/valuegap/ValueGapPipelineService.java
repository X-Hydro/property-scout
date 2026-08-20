package com.oncoord.propertyscout.valuegap;

import com.oncoord.propertyscout.model.Listing;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * Wires the two halves together for a live listing: resolve its parcel and
 * candidate comps (NearbyCompsService), then compute the gap
 * (GapComputationService). This is the live-call equivalent of running
 * find_abutters.py then compute_gap.py against its output -- same two
 * stages, just without the intermediate GeoJSON files.
 */
@Service
public class ValueGapPipelineService {

    private final NearbyCompsService nearbyCompsService;
    private final GapComputationService gapComputationService;
    private final GapRankingService gapRankingService;

    public ValueGapPipelineService(
            NearbyCompsService nearbyCompsService,
            GapComputationService gapComputationService,
            GapRankingService gapRankingService) {
        this.nearbyCompsService = nearbyCompsService;
        this.gapComputationService = gapComputationService;
        this.gapRankingService = gapRankingService;
    }

    /**
     * Full pipeline for one listing. Empty if the listing's point doesn't
     * resolve to a parcel at all, OR if it resolves to one that fails the
     * implausible-size sanity check (see NearbyCompsService.resolveTargetParcel).
     * Neither case falls back to address matching -- see that method's docs.
     */
    public Optional<GapResult> computeForListing(Listing listing) {
        if (listing.getLatitude() == null || listing.getLongitude() == null) {
            return Optional.empty();
        }

        Optional<TargetParcel> target = nearbyCompsService.resolveTargetParcel(
                listing.getLatitude(), listing.getLongitude(), listing.getLotSize());
        if (target.isEmpty()) {
            return Optional.empty();
        }

        boolean targetIsLand = "Land".equals(listing.getPropertyType());
        List<CompCandidate> candidates = nearbyCompsService.findComps(target.get(), targetIsLand);

        GapResult result = gapComputationService.compute(
                listing.getListingId(),
                listing.getFormattedAddress(),
                listing.getPropertyType(),
                listing.getYearBuilt(),
                listing.getPrice(),
                target.get().getAssessedValue(),
                candidates
        );

        return Optional.of(result);
    }

    /**
     * Runs computeForListing over a batch and hands the results to
     * GapRankingService. Listings that don't resolve to a parcel are simply
     * absent from the output -- same behavior as compute_gap.py silently
     * dropping listings find_abutters.py couldn't resolve.
     */
    public GapRankingService.RankedGaps rankListings(List<Listing> listings) {
        List<GapResult> results = new ArrayList<>();
        for (Listing listing : listings) {
            computeForListing(listing).ifPresent(results::add);
        }
        return gapRankingService.rank(results);
    }
}