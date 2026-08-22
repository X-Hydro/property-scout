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

    /**
     * Truncates each property-type group to its top `limit` results (by
     * gap, largest first). Applied AFTER rank() -- gap has to be computed
     * for every matching listing regardless of limit, since there's no
     * cheap proxy for "biggest gap" other than actually running the comp
     * analysis; this only trims what gets serialized in the response, for
     * callers like a statewide "biggest gaps, don't care what town" view
     * where returning every matching listing isn't the point.
     *
     * A null limit is a no-op (returns the input map as-is) -- existing
     * city-scoped callers that don't pass limit keep today's behavior
     * (every matching listing returned) unchanged.
     */
    public static Map<String, List<GapResult>> applyLimit(
            Map<String, List<GapResult>> rankedByPropertyType, Integer limit) {
        if (limit == null) {
            return rankedByPropertyType;
        }
        Map<String, List<GapResult>> limited = new LinkedHashMap<>();
        for (Map.Entry<String, List<GapResult>> entry : rankedByPropertyType.entrySet()) {
            List<GapResult> group = entry.getValue();
            limited.put(entry.getKey(),
                    group.size() > limit ? new ArrayList<>(group.subList(0, limit)) : group);
        }
        return limited;
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