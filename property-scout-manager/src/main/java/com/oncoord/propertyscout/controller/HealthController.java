package com.oncoord.propertyscout.controller;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * Narrowest possible first slice: prove the Java service can actually
 * reach the real property-scout Postgres/PostGIS database and run a
 * real query, before anything else (auth, the ported gap-analysis SQL,
 * RentCast live-fetch, etc.) gets built on top of it.
 *
 * GET /health         -- plain liveness check, no DB involved
 * GET /demo/lincoln-nh/count -- a real JdbcTemplate query against
 *                                property_values, proving the JDBC
 *                                connection and PostGIS-backed table
 *                                are both reachable end-to-end
 */
@RestController
public class HealthController {

    private final JdbcTemplate jdbcTemplate;

    @Autowired
    public HealthController(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok");
    }

    @GetMapping("/demo/lincoln-nh/count")
    public Map<String, Object> lincolnCount() {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM property_values WHERE state = ? AND municipality ILIKE ?",
                Integer.class, "NH", "Lincoln"
        );
        return Map.of("state", "NH", "municipality", "Lincoln", "property_values_count", count);
    }
}