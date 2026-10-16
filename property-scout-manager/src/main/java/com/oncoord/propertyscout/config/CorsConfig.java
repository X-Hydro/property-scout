package com.oncoord.propertyscout.config;


import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;
import org.springframework.web.filter.CorsFilter;

import java.util.Arrays;

/**
 * CORS as a servlet filter that runs BEFORE oncoord-accounts' ApiAuthFilter
 * (which is at HIGHEST_PRECEDENCE + 10), so 401/403 responses still carry
 * CORS headers and the frontend can see them and redirect to login.
 *
 * X-Admin-Key is deliberately NOT an allowed header, so no web page can send
 * the admin key cross-origin. Admin calls are curl/scripts only.
 */
@Configuration
public class CorsConfig {

    @Bean
    public FilterRegistrationBean<CorsFilter> corsFilter() {
        CorsConfiguration config = new CorsConfiguration();

        config.setAllowedOrigins(Arrays.asList(
                "http://localhost:8080",
                "https://oncoord.com",
                "https://www.oncoord.com",
                "https://api.oncoord.com"
        ));

        config.setAllowedMethods(Arrays.asList("GET", "POST", "PUT", "DELETE", "OPTIONS"));
        config.setAllowedHeaders(Arrays.asList("Content-Type", "X-Session-Token"));
        // No setAllowCredentials(true): the session token travels in a header, not a cookie.
        config.setMaxAge(3600L);

        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", config);

        FilterRegistrationBean<CorsFilter> reg = new FilterRegistrationBean<>(new CorsFilter(source));
        reg.setOrder(Ordered.HIGHEST_PRECEDENCE);
        return reg;
    }
}