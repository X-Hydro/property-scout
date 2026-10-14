package com.oncoord.propertyscout.config;

import com.oncoord.auth.common.RecaptchaClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Unlike auction-scout-manager's AuthConfig, there are no TokenStore /
 * TokenService beans here: oncoord-accounts' AuthService (auto-configured)
 * builds its own, backed by the hashed accounts.tokens table.
 */
@Configuration
public class AuthConfig {

    @Bean
    public RecaptchaClient recaptchaClient(@Value("${recaptcha.secret-key}") String secret) {
        return new RecaptchaClient(secret);
    }
}
