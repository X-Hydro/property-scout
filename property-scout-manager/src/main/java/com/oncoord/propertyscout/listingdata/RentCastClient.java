package com.oncoord.propertyscout.listingdata;


import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClientResponseException;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.UriComponentsBuilder;

import java.util.Map;
import java.util.TreeMap;

/**
 * Raw RentCast API access — every method here spends money. Nothing calls
 * this directly except {@link RentCastService}; everything else should go
 * through the cache.
 */
@Component
public class RentCastClient {

    private final RestTemplate restTemplate;
    private final String apiKey;
    private final String baseUrl;

    public RentCastClient(
            RestTemplate restTemplate,
            @Value("${rentcast.api-key}") String apiKey,
            @Value("${rentcast.base-url:https://api.rentcast.io/v1}") String baseUrl) {

        this.restTemplate = restTemplate;
        this.apiKey = apiKey;
        this.baseUrl = baseUrl;
    }

    /**
     * @param endpoint    RentCast path relative to the base URL, e.g.
     *                    "avm/value" or "listings/sale"
     * @param queryParams query params for the call, e.g. address/lat/lon/radius
     */
    public RentCastRawResponse call(String endpoint, Map<String, String> queryParams) {
        // TreeMap so the same logical request always builds the same URL,
        // which matters for RentCastService's cache key too.
        Map<String, String> sorted = new TreeMap<>(queryParams);

        UriComponentsBuilder builder = UriComponentsBuilder
                .fromHttpUrl(baseUrl)
                .path("/" + endpoint.replaceFirst("^/", ""));
        sorted.forEach(builder::queryParam);

        HttpHeaders headers = new HttpHeaders();
        headers.set("X-Api-Key", apiKey);
        headers.set("Accept", "application/json");

        try {
            ResponseEntity<String> response = restTemplate.exchange(
                    builder.build().toUri(),
                    HttpMethod.GET,
                    new HttpEntity<>(headers),
                    String.class
            );
            return new RentCastRawResponse(response.getStatusCode().value(), response.getBody());
        } catch (RestClientResponseException e) {
            // Still return a structured result so RentCastService can decide
            // whether/how to cache 4xx responses (e.g. "no data for this
            // address" is worth caching so we don't pay to ask again).
            return new RentCastRawResponse(e.getStatusCode().value(), e.getResponseBodyAsString());
        }
    }

    public record RentCastRawResponse(int statusCode, String body) {
    }
}