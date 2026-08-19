package com.oncoord.propertyscout.config;


import org.springframework.boot.web.client.RestTemplateBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.client.RestTemplate;

import java.time.Duration;

/**
 * Spring Boot autoconfigures RestTemplateBuilder but deliberately does NOT
 * register a RestTemplate bean itself (multiple RestTemplates with
 * different configs are a common need, so it leaves that choice to the
 * app). RentCastClient -- and any future *Client for another
 * ListingDataProvider implementation -- depends on RestTemplate directly,
 * so one shared bean is registered here.
 */
@Configuration
public class RestClientConfig {

    @Bean
    public RestTemplate restTemplate(RestTemplateBuilder builder) {
        return builder
                .setConnectTimeout(Duration.ofSeconds(5))
                .setReadTimeout(Duration.ofSeconds(15))
                .build();
    }
}