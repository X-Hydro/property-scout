package com.oncoord.propertyscout.listingdata;


import com.oncoord.propertyscout.model.ListingSourceCacheEntry;
import com.oncoord.propertyscout.service.ListingSourceCacheService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.Map;
import java.util.Optional;
import java.util.TreeMap;

/**
 * Public entry point for any code that wants RentCast data. Always checks
 * the shared listing_source_cache first (tagged source='rentcast'); only
 * calls out to RentCast, and pays for it, on a cache miss or an expired
 * entry. If RentCast is ever swapped for another provider, that provider
 * gets its own *Client + *Service pair using the same
 * ListingSourceCacheService with a different source tag -- the cache
 * table itself doesn't need to change.
 *
 * Usage:
 *   ListingSourceResult result = rentCastService.fetch(
 *       "avm/value",
 *       Map.of("latitude", "43.9695", "longitude", "-71.6867", "radius", "0.5"),
 *       Duration.ofDays(30));
 */
@Service
public class RentCastService {

    private static final String SOURCE = "rentcast";

    private final RentCastClient client;
    private final ListingSourceCacheService cacheService;
    private final Duration defaultTtl;

    public RentCastService(
            RentCastClient client,
            ListingSourceCacheService cacheService,
            @Value("${rentcast.cache-ttl-days:30}") long defaultTtlDays) {

        this.client = client;
        this.cacheService = cacheService;
        this.defaultTtl = Duration.ofDays(defaultTtlDays);
    }

    public ListingSourceResult fetch(String endpoint, Map<String, String> params) {
        return fetch(endpoint, params, defaultTtl);
    }

    /**
     * @param ttl how long a fresh result is considered valid. Pass
     *            Duration.ZERO or a negative duration for "never expires" --
     *            useful for historical data (e.g. a specific past sale)
     *            that won't change.
     */
    public ListingSourceResult fetch(String endpoint, Map<String, String> params, Duration ttl) {
        String requestKey = buildRequestKey(params);

        Optional<ListingSourceCacheEntry> cached = cacheService.find(SOURCE, endpoint, requestKey);
        OffsetDateTime now = OffsetDateTime.now();

        if (cached.isPresent() && !cached.get().isExpired(now)) {
            ListingSourceCacheEntry entry = cached.get();
            cacheService.recordHit(entry.getId());
            return new ListingSourceResult(entry.getStatusCode(), entry.getResponseBodyJson(), true);
        }

        RentCastClient.RentCastRawResponse raw = client.call(endpoint, params);

        OffsetDateTime expiresAt = ttl == null || ttl.isZero() || ttl.isNegative()
                ? null
                : now.plus(ttl);

        cacheService.save(
                SOURCE,
                endpoint,
                requestKey,
                toJsonObject(params),
                raw.body() == null ? "null" : raw.body(),
                raw.statusCode(),
                expiresAt
        );

        return new ListingSourceResult(raw.statusCode(), raw.body(), false);
    }

    /**
     * Stable key so identical requests (regardless of param insertion order)
     * hit the same cache row. Params are sorted, joined, and SHA-256 hashed
     * so the request_key column stays a fixed, index-friendly length.
     */
    private String buildRequestKey(Map<String, String> params) {
        Map<String, String> sorted = new TreeMap<>(params);
        StringBuilder canonical = new StringBuilder();
        sorted.forEach((k, v) -> canonical.append(k).append('=').append(v).append('&'));

        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(canonical.toString().getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte b : hash) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            // SHA-256 is always available on the JVM; this is unreachable.
            throw new IllegalStateException(e);
        }
    }

    private String toJsonObject(Map<String, String> params) {
        StringBuilder json = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, String> entry : new TreeMap<>(params).entrySet()) {
            if (!first) {
                json.append(',');
            }
            first = false;
            json.append('"').append(escape(entry.getKey())).append('"')
                    .append(':')
                    .append('"').append(escape(entry.getValue())).append('"');
        }
        return json.append('}').toString();
    }

    private String escape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    public record ListingSourceResult(int statusCode, String body, boolean fromCache) {
    }
}