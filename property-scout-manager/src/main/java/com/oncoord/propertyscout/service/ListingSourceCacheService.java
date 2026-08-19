package com.oncoord.propertyscout.service;

import com.oncoord.propertyscout.model.ListingSourceCacheEntry;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.util.Optional;

/**
 * Generic cache over listing_source_cache, shared by whichever paid
 * listing-data provider is active (RentCast today) -- keyed by
 * (source, endpoint, request_key) so more than one provider can use this
 * same table/service without a rename. Holds JdbcTemplate directly, same
 * as ListingsService/PropertyValuesService -- no separate repository layer.
 */
@Service
public class ListingSourceCacheService {

    private final JdbcTemplate jdbcTemplate;

    public ListingSourceCacheService(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Optional<ListingSourceCacheEntry> find(String source, String endpoint, String requestKey) {
        String sql = """
            SELECT id, source, endpoint, request_key, request_params::text, response_body::text,
                   status_code, fetched_at, expires_at
            FROM listing_source_cache
            WHERE source = ? AND endpoint = ? AND request_key = ?
            """;

        return jdbcTemplate.query(sql, rs -> {
            if (!rs.next()) {
                return Optional.empty();
            }
            ListingSourceCacheEntry entry = new ListingSourceCacheEntry();
            entry.setId(rs.getLong("id"));
            entry.setSource(rs.getString("source"));
            entry.setEndpoint(rs.getString("endpoint"));
            entry.setRequestKey(rs.getString("request_key"));
            entry.setRequestParamsJson(rs.getString("request_params"));
            entry.setResponseBodyJson(rs.getString("response_body"));
            entry.setStatusCode(rs.getInt("status_code"));
            entry.setFetchedAt(rs.getObject("fetched_at", OffsetDateTime.class));
            entry.setExpiresAt(rs.getObject("expires_at", OffsetDateTime.class));
            return Optional.of(entry);
        }, source, endpoint, requestKey);
    }

    /**
     * Upsert on (source, endpoint, request_key). Called after a live call
     * to whichever listing-data provider is active, so the next lookup
     * with the same params is served from cache.
     */
    public void save(
            String source,
            String endpoint,
            String requestKey,
            String requestParamsJson,
            String responseBodyJson,
            int statusCode,
            OffsetDateTime expiresAt) {

        String sql = """
            INSERT INTO listing_source_cache
                (source, endpoint, request_key, request_params, response_body, status_code, fetched_at, expires_at)
            VALUES (?, ?, ?, ?::jsonb, ?::jsonb, ?, now(), ?)
            ON CONFLICT (source, endpoint, request_key)
            DO UPDATE SET
                request_params = EXCLUDED.request_params,
                response_body = EXCLUDED.response_body,
                status_code = EXCLUDED.status_code,
                fetched_at = now(),
                expires_at = EXCLUDED.expires_at
            """;

        jdbcTemplate.update(
                sql,
                source,
                endpoint,
                requestKey,
                requestParamsJson,
                responseBodyJson,
                statusCode,
                expiresAt
        );
    }

    public void recordHit(long cacheEntryId) {
        jdbcTemplate.update(
                "UPDATE listing_source_cache SET hit_count = hit_count + 1, last_hit_at = now() WHERE id = ?",
                cacheEntryId
        );
    }
}