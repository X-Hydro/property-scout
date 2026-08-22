package com.oncoord.propertyscout.valuegap;

import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Mutable state for one background gap-ranking run, polled by the frontend
 * via GET /api/value-gap/rank/jobs/{jobId}. Exists because a statewide run
 * (thousands of listings, each needing its own comp search) is too slow for
 * a single synchronous request/response -- see GapAnalysisJobService for
 * how this gets populated.
 *
 * Tracks progress at TWO granularities: listings (processed/total) and
 * cities (citiesProcessed/totalCities). The frontend only displays the
 * city-level numbers ("processing city 12 of 169") -- listing counts are
 * kept too since they're needed internally to detect when a city's last
 * listing has finished (see GapAnalysisJobService), and cost nothing extra
 * to expose.
 *
 * Thread-safety: processed/citiesProcessed/currentCity are written from
 * background worker threads and read from HTTP request threads polling
 * status, so all are safe for concurrent access (AtomicInteger, volatile)
 * without needing a lock.
 */
public class GapAnalysisJob {

    public enum Status { RUNNING, COMPLETED, FAILED }

    private volatile Status status = Status.RUNNING;
    private final AtomicInteger processed = new AtomicInteger(0);
    private final int total;
    private final AtomicInteger citiesProcessed = new AtomicInteger(0);
    private final int totalCities;
    private volatile String currentCity;
    private volatile Map<String, Object> result;
    private volatile String errorMessage;
    private volatile boolean cancelRequested = false;

    public void requestCancel() {
        cancelRequested = true;
    }

    public boolean isCancelRequested() {
        return cancelRequested;
    }

    public GapAnalysisJob(int total, int totalCities) {
        this.total = total;
        this.totalCities = totalCities;
    }

    /** Called once per listing as the background loop processes it. */
    public void recordProgress(String city) {
        this.currentCity = city;
        processed.incrementAndGet();
    }

    /** Called once per city, when that city's last listing finishes. */
    public void recordCityComplete() {
        citiesProcessed.incrementAndGet();
    }

    public void complete(Map<String, Object> result) {
        this.result = result;
        this.status = Status.COMPLETED;
    }

    public void fail(String message) {
        this.errorMessage = message;
        this.status = Status.FAILED;
    }

    public Status getStatus() {
        return status;
    }

    public int getProcessed() {
        return processed.get();
    }

    public int getTotal() {
        return total;
    }

    public int getCitiesProcessed() {
        return citiesProcessed.get();
    }

    public int getTotalCities() {
        return totalCities;
    }

    public String getCurrentCity() {
        return currentCity;
    }

    public Map<String, Object> getResult() {
        return result;
    }

    public String getErrorMessage() {
        return errorMessage;
    }
}