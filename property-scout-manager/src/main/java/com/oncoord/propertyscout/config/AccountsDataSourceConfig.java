package com.oncoord.propertyscout.config;

import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;

@Configuration
public class AccountsDataSourceConfig {

    // Bean name must be "accountsJdbcTemplate" -- oncoord-accounts looks it up by name.
    @Bean
    NamedParameterJdbcTemplate accountsJdbcTemplate(
            @Value("${ACCOUNTS_DB_URL}") String url,
            @Value("${DB_USER}") String user,
            @Value("${DB_PASSWORD}") String password) {
        HikariDataSource ds = new HikariDataSource();
        ds.setJdbcUrl(url);
        ds.setUsername(user);
        ds.setPassword(password);
        ds.setPoolName("accounts");
        ds.setMaximumPoolSize(5);
        return new NamedParameterJdbcTemplate(ds);
    }
}