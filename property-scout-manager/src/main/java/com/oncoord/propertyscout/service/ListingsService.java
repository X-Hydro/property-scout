package com.oncoord.propertyscout.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.propertyscout.mapper.ListingRowMapper;
import com.oncoord.propertyscout.model.Listing;
import com.oncoord.propertyscout.model.StateCityRec;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

@Service
public class ListingsService {

    private final JdbcTemplate jdbcTemplate;
    private final ListingRowMapper listingRowMapper;
    private final ObjectMapper objectMapper;

    private static final String SELECT_COLUMNS = """
            SELECT
                listing_id,
                formatted_address,
                address_line_1,
                address_line_2,
                city,
                state,
                zip_code,
                county,
                latitude,
                longitude,
                property_type,
                bedrooms,
                bathrooms,
                square_footage,
                lot_size,
                year_built,
                status,
                price,
                listing_type,
                listed_date,
                removed_date,
                days_on_market,
                mls_name,
                mls_number,
                agent,
                office,
                price_history,
                source,
                fetched_at,
                listing_url,
                primary_photo
            FROM listings
            """;

    private static final String UPSERT_SQL = """
            INSERT INTO listings (
                listing_id, formatted_address, address_line_1, address_line_2,
                city, state, zip_code, county, latitude, longitude,
                property_type, bedrooms, bathrooms, square_footage, lot_size, year_built,
                status, price, listing_type, listed_date, removed_date, days_on_market,
                mls_name, mls_number, agent, office, price_history, source, fetched_at,
                listing_url, primary_photo
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?::jsonb, ?::jsonb, ?::jsonb, ?, ?,
                ?, ?
            )
            ON CONFLICT (listing_id) DO UPDATE SET
                formatted_address = EXCLUDED.formatted_address,
                address_line_1 = EXCLUDED.address_line_1,
                address_line_2 = EXCLUDED.address_line_2,
                city = EXCLUDED.city,
                state = EXCLUDED.state,
                zip_code = EXCLUDED.zip_code,
                county = EXCLUDED.county,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                property_type = EXCLUDED.property_type,
                bedrooms = EXCLUDED.bedrooms,
                bathrooms = EXCLUDED.bathrooms,
                square_footage = EXCLUDED.square_footage,
                lot_size = EXCLUDED.lot_size,
                year_built = EXCLUDED.year_built,
                status = EXCLUDED.status,
                price = EXCLUDED.price,
                listing_type = EXCLUDED.listing_type,
                listed_date = EXCLUDED.listed_date,
                removed_date = EXCLUDED.removed_date,
                days_on_market = EXCLUDED.days_on_market,
                mls_name = EXCLUDED.mls_name,
                mls_number = EXCLUDED.mls_number,
                agent = EXCLUDED.agent,
                office = EXCLUDED.office,
                price_history = EXCLUDED.price_history,
                source = EXCLUDED.source,
                fetched_at = EXCLUDED.fetched_at,
                listing_url = EXCLUDED.listing_url,
                primary_photo = EXCLUDED.primary_photo
            """;

    public ListingsService(
            JdbcTemplate jdbcTemplate,
            ListingRowMapper listingRowMapper,
            ObjectMapper objectMapper) {

        this.jdbcTemplate = jdbcTemplate;
        this.listingRowMapper = listingRowMapper;
        this.objectMapper = objectMapper;
    }

    public List<Listing> findListings(
            String state,
            String city,
            String zipCode,
            String propertyType) {

        return findListings(state, city, zipCode, propertyType, null);
    }

    /**
     * @param since when non-null, restricts to listings with
     *              fetched_at >= since (start of that day) -- e.g. to
     *              recompute only what a targeted incremental scrape just
     *              touched instead of the whole state.
     */
    public List<Listing> findListings(
            String state,
            String city,
            String zipCode,
            String propertyType,
            LocalDate since) {

        StringBuilder sql = new StringBuilder(SELECT_COLUMNS + " WHERE UPPER(state) = UPPER(?)");

        List<Object> parameters = new ArrayList<>();
        parameters.add(state);

        if (city != null && !city.isBlank()) {
            sql.append(" AND UPPER(city) = UPPER(?)");
            parameters.add(city);
        }

        if (zipCode != null && !zipCode.isBlank()) {
            sql.append(" AND zip_code = ?");
            parameters.add(zipCode);
        }

        if (propertyType != null && !propertyType.isBlank()) {
            sql.append(" AND UPPER(property_type) = UPPER(?)");
            parameters.add(propertyType);
        }

        if (since != null) {
            // atStartOfDay so this is correct whether fetched_at is a
            // date or timestamp column -- comparing a bare LocalDate
            // param against a timestamp column can otherwise exclude
            // same-day rows depending on time-of-day.
            sql.append(" AND fetched_at >= ?");
            parameters.add(LocalDateTime.of(since, java.time.LocalTime.MIDNIGHT));
        }

        sql.append(" ORDER BY listed_date DESC NULLS LAST");

        return jdbcTemplate.query(
                sql.toString(),
                listingRowMapper,
                parameters.toArray()
        );
    }

    /** Single listing by its RentCast id, for driving the ValueGap pipeline on one target. */
    public Optional<Listing> findById(String listingId) {
        List<Listing> results = jdbcTemplate.query(
                SELECT_COLUMNS + " WHERE listing_id = ?",
                listingRowMapper,
                listingId
        );
        return results.stream().findFirst();
    }

    /**
     * Upsert on listing_id, one statement per listing. Fine at the batch
     * sizes a single town/state pull returns (dozens to low hundreds);
     * worth switching to jdbcTemplate.batchUpdate if ingestion ever scales
     * to pulling many states at once.
     *
     * @return number of listings upserted
     */
    public int upsertAll(List<Listing> listings) {
        for (Listing l : listings) {
            jdbcTemplate.update(
                    UPSERT_SQL,
                    l.getListingId(),
                    l.getFormattedAddress(),
                    l.getAddressLine1(),
                    l.getAddressLine2(),
                    l.getCity(),
                    l.getState(),
                    l.getZipCode(),
                    l.getCounty(),
                    l.getLatitude(),
                    l.getLongitude(),
                    l.getPropertyType(),
                    l.getBedrooms(),
                    l.getBathrooms(),
                    l.getSquareFootage(),
                    l.getLotSize(),
                    l.getYearBuilt(),
                    l.getStatus(),
                    l.getPrice(),
                    l.getListingType(),
                    l.getListedDate(),
                    l.getRemovedDate(),
                    l.getDaysOnMarket(),
                    l.getMlsName(),
                    l.getMlsNumber(),
                    toJsonText(l.getAgent()),
                    toJsonText(l.getOffice()),
                    toJsonText(l.getPriceHistory()),
                    l.getSource(),
                    l.getFetchedAt(),
                    l.getListingUrl(),
                    l.getPrimaryPhoto()
            );
        }
        return listings.size();
    }

    /**
     * JsonNode -> JSON text for the ?::jsonb parameters above. A plain
     * PreparedStatement can't bind a JsonNode directly; passing the text
     * form and letting Postgres cast it is the simplest path that doesn't
     * need a custom JDBC type/PGobject wiring.
     */
    private String toJsonText(JsonNode node) {
        if (node == null || node.isNull()) {
            return null;
        }
        return node.toString();
    }

    public List<StateCityRec> getStateCity() {
        //Note:  This is joinedto the gap_results table as we don't want
        //a user to be able to select a state or state/city that has not been
        //analyzed yet.
        return jdbcTemplate.query(
                "SELECT DISTINCT l.state, l.city " +
                        "FROM listings l " +
                        "WHERE EXISTS (" +
                        "    SELECT 1 FROM gap_results g WHERE g.listing_id = l.listing_id" +
                        ") " +
                        "ORDER BY l.state, l.city",
                (rs, rowNum) -> new StateCityRec(rs.getString("state"), rs.getString("city"))
        );
    }
}