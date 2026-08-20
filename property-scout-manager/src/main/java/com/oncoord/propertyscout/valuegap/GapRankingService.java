package com.oncoord.propertyscout.valuegap;

import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Part 3: given every GapResult for a batch of listings, group by property
 * type, sort largest-gap-first within each group, and set relativeGapPct
 * (gapPct minus that group's median gapPct) on each result. Port of
 * compute_gap.py's run_rank_mode() sorting/grouping logic -- not the CSV/
 * console output, just the math and ordering, which is the reusable part.
 *
 * Results with no usable comps (hasComps == false) are returned separately,
 * unranked, same as compute_gap.py's "no_comps" bucket.
 */
@Service
public class GapRankingService {

    public static class RankedGaps {
        private final Map<String, List<GapResult>> rankedByPropertyType;
        private final List<GapResult> noComps;

        public RankedGaps(Map<String, List<GapResult>> rankedByPropertyType, List<GapResult> noComps) {
            this.rankedByPropertyType = rankedByPropertyType;
            this.noComps = noComps;
        }

        public Map<String, List<GapResult>> getRankedByPropertyType() {
            return rankedByPropertyType;
        }

        public List<GapResult> getNoComps() {
            return noComps;
        }
    }

    public RankedGaps rank(List<GapResult> results) {
        Map<String, List<GapResult>> byType = new LinkedHashMap<>();
        List<GapResult> noComps = new ArrayList<>();

        for (GapResult r : results) {
            if (!r.isHasComps()) {
                noComps.add(r);
                continue;
            }
            byType.computeIfAbsent(r.getPropertyType(), k -> new ArrayList<>()).add(r);
        }

        for (List<GapResult> group : byType.values()) {
            group.sort(Comparator.comparingDouble(GapResult::getGap).reversed());
            addRelativeGap(group);
        }

        return new RankedGaps(byType, noComps);
    }

    private void addRelativeGap(List<GapResult> group) {
        if (group.isEmpty()) {
            return;
        }
        List<Double> pcts = new ArrayList<>();
        for (GapResult r : group) {
            pcts.add(r.getGapPct());
        }
        pcts.sort(Double::compareTo);
        int n = pcts.size();
        double groupMedian = (n % 2 == 1)
                ? pcts.get(n / 2)
                : (pcts.get(n / 2 - 1) + pcts.get(n / 2)) / 2.0;

        for (GapResult r : group) {
            r.setRelativeGapPct(r.getGapPct() - groupMedian);
        }
    }
}