package com.oncoord.propertyscout.geo;

import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.MultiPolygon;
import org.locationtech.jts.geom.Polygon;

/**
 * Geometry-quality checks used to screen candidate parcels before they're
 * treated as legitimate value comps. Distance methods use a local
 * flat-earth (equirectangular) approximation anchored at a reference
 * latitude -- the same approximation find_abutters.py's own distance_m()
 * uses (already accepted as good enough at this scale, see project notes
 * on the geodesic-vs-flat-earth comp-count discrepancy). Not meant for
 * anything requiring true geodesic accuracy.
 */
public final class ParcelGeometryUtils {

    private static final double METERS_PER_DEGREE_LAT = 111_320.0;

    private ParcelGeometryUtils() {
    }

    /** Number of polygon parts -- 1 for a plain Polygon, N for a MultiPolygon, 0 otherwise. */
    public static int countMultiPoly(Geometry geom) {
        if (geom == null) {
            return 0;
        }
        if (geom instanceof MultiPolygon) {
            return geom.getNumGeometries();
        }
        return geom instanceof Polygon ? 1 : 0;
    }

    /** Total interior rings (holes) summed across every polygon part. */
    public static int countHoles(Geometry geom) {
        if (geom == null) {
            return 0;
        }
        int holes = 0;
        for (int i = 0; i < geom.getNumGeometries(); i++) {
            if (geom.getGeometryN(i) instanceof Polygon poly) {
                holes += poly.getNumInteriorRing();
            }
        }
        return holes;
    }

    /** Distance in meters from the nearest vertex of {@code geom} to (refLat, refLon). */
    public static double closestPointDistanceM(Geometry geom, double refLat, double refLon) {
        return vertexDistanceM(geom, refLat, refLon, true);
    }

    /** Distance in meters from the farthest vertex of {@code geom} to (refLat, refLon). */
    public static double furthestPointDistanceM(Geometry geom, double refLat, double refLon) {
        return vertexDistanceM(geom, refLat, refLon, false);
    }

    private static double vertexDistanceM(Geometry geom, double refLat, double refLon, boolean nearest) {
        if (geom == null || geom.isEmpty()) {
            return nearest ? Double.POSITIVE_INFINITY : 0.0;
        }
        double metersPerDegreeLon = METERS_PER_DEGREE_LAT * Math.cos(Math.toRadians(refLat));
        double best = nearest ? Double.POSITIVE_INFINITY : Double.NEGATIVE_INFINITY;
        for (Coordinate c : geom.getCoordinates()) {
            double dLat = (c.y - refLat) * METERS_PER_DEGREE_LAT;
            double dLon = (c.x - refLon) * metersPerDegreeLon;
            double d = Math.sqrt(dLat * dLat + dLon * dLon);
            best = nearest ? Math.min(best, d) : Math.max(best, d);
        }
        return best;
    }
}