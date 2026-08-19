package com.oncoord.propertyscout.valuegap;

import java.util.Map;
import java.util.regex.Pattern;

/**
 * Direct ports of the small pure-Python helpers in find_abutters.py /
 * compute_gap.py: address normalization, street-name-only extraction, and
 * the lot-size-ratio comparability check. Kept identical to the Python
 * versions on purpose so Java and Python results don't quietly diverge.
 */
public final class ValueGapUtils {

    private ValueGapUtils() {
    }

    private static final Map<String, String> SUFFIX_MAP = Map.ofEntries(
            Map.entry("ROAD", "RD"), Map.entry("STREET", "ST"), Map.entry("LANE", "LN"),
            Map.entry("DRIVE", "DR"), Map.entry("AVENUE", "AVE"), Map.entry("MOUNTAIN", "MTN"),
            Map.entry("TRAIL", "TRL"), Map.entry("CIRCLE", "CIR"), Map.entry("COURT", "CT"),
            Map.entry("BOULEVARD", "BLVD"), Map.entry("HIGHWAY", "HWY"), Map.entry("PLACE", "PL")
    );

    private static final Pattern NON_WORD = Pattern.compile("[^\\w\\s]");
    private static final Pattern MULTI_SPACE = Pattern.compile("\\s+");

    public static String normalizeAddress(String raw) {
        if (raw == null || raw.isBlank()) {
            return "";
        }
        String s = NON_WORD.matcher(raw.toUpperCase()).replaceAll(" ");
        s = MULTI_SPACE.matcher(s).replaceAll(" ").trim();
        if (s.isEmpty()) {
            return "";
        }
        StringBuilder out = new StringBuilder();
        for (String token : s.split(" ")) {
            if (!out.isEmpty()) {
                out.append(" ");
            }
            out.append(SUFFIX_MAP.getOrDefault(token, token));
        }
        return out.toString();
    }

    /**
     * Normalized street name with any leading house number stripped, so
     * "184 CROOKED MTN ROAD" and "CROOKED MOUNTAIN ROAD #101" both reduce to
     * "CROOKED MTN RD". Exact match on the normalized form, not fuzzy.
     */
    public static String streetNameOnly(String streetAddress) {
        String normalized = normalizeAddress(streetAddress);
        if (normalized.isEmpty()) {
            return "";
        }
        String[] tokens = normalized.split(" ");
        if (tokens.length > 0 && tokens[0].chars().allMatch(Character::isDigit)) {
            StringBuilder out = new StringBuilder();
            for (int i = 1; i < tokens.length; i++) {
                if (!out.isEmpty()) {
                    out.append(" ");
                }
                out.append(tokens[i]);
            }
            return out.toString();
        }
        return normalized;
    }

    /**
     * True if the larger of the two lot sizes is no more than ratioTolerance
     * times the smaller. Default tolerance in the Python scripts is 2.5.
     */
    public static boolean lotSizeSimilar(Double acresA, Double acresB, double ratioTolerance) {
        if (acresA == null || acresB == null || acresA <= 0 || acresB <= 0) {
            return false;
        }
        double lo = Math.min(acresA, acresB);
        double hi = Math.max(acresA, acresB);
        return (hi / lo) <= ratioTolerance;
    }
}