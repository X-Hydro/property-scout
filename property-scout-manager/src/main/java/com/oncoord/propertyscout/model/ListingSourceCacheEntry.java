package com.oncoord.propertyscout.model;

import java.time.OffsetDateTime;

public class ListingSourceCacheEntry {

    private long id;
    private String source;
    private String endpoint;
    private String requestKey;
    private String requestParamsJson;
    private String responseBodyJson;
    private int statusCode;
    private OffsetDateTime fetchedAt;
    private OffsetDateTime expiresAt;

    public ListingSourceCacheEntry() {
    }

    public boolean isExpired(OffsetDateTime now) {
        return expiresAt != null && expiresAt.isBefore(now);
    }

    public long getId() {
        return id;
    }

    public void setId(long id) {
        this.id = id;
    }

    public String getSource() {
        return source;
    }

    public void setSource(String source) {
        this.source = source;
    }

    public String getEndpoint() {
        return endpoint;
    }

    public void setEndpoint(String endpoint) {
        this.endpoint = endpoint;
    }

    public String getRequestKey() {
        return requestKey;
    }

    public void setRequestKey(String requestKey) {
        this.requestKey = requestKey;
    }

    public String getRequestParamsJson() {
        return requestParamsJson;
    }

    public void setRequestParamsJson(String requestParamsJson) {
        this.requestParamsJson = requestParamsJson;
    }

    public String getResponseBodyJson() {
        return responseBodyJson;
    }

    public void setResponseBodyJson(String responseBodyJson) {
        this.responseBodyJson = responseBodyJson;
    }

    public int getStatusCode() {
        return statusCode;
    }

    public void setStatusCode(int statusCode) {
        this.statusCode = statusCode;
    }

    public OffsetDateTime getFetchedAt() {
        return fetchedAt;
    }

    public void setFetchedAt(OffsetDateTime fetchedAt) {
        this.fetchedAt = fetchedAt;
    }

    public OffsetDateTime getExpiresAt() {
        return expiresAt;
    }

    public void setExpiresAt(OffsetDateTime expiresAt) {
        this.expiresAt = expiresAt;
    }
}