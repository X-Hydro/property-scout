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
 * Thread-safety: processed/currentCity are written from the background job
 * thread and read from HTTP request threads polling status, so both are
 * safe for concurrent access (AtomicInteger, volatile) without needing a
 * lock -- there's only ever one writer per job.
 */
public class GapAnalysisJob {

    public enum Status { RUNNING, COMPLETED, FAILED }

    private volatile Status status = Status.RUNNING;
    private final AtomicInteger processed = new AtomicInteger(0);
    private final int total;
    private volatile String currentCity;
    private volatile Map<String, Object> result;
    private volatile String errorMessage;

    public GapAnalysisJob(int total) {
        this.total = total;
    }

    /** Called once per listing as the background loop processes it. */
    public void recordProgress(String city) {
        this.currentCity = city;
        processed.incrementAndGet();
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