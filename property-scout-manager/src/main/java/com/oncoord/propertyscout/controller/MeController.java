package com.oncoord.propertyscout.controller;

import com.oncoord.accounts.security.ApiAuthFilter;
import com.oncoord.accounts.user.User;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.HashMap;
import java.util.Map;

/**
 * GET /api/me: "is my session still valid, and who am I?"
 *
 * It sits under /api/ but not /api/auth/, so ApiAuthFilter has already
 * rejected the request with a 401 if the session is missing or expired.
 * Reaching this method means the session is valid.
 */
@RestController
public class MeController {

    @GetMapping("/api/me")
    public Map<String, Object> me(HttpServletRequest request) {
        User user = ApiAuthFilter.currentUser(request)
                .orElseThrow(() -> new IllegalStateException("ApiAuthFilter did not run for /api/me"));
        // HashMap, not Map.of: username can be null (accounts from before
        // usernames, or a new user who signed in without creating an account).
        Map<String, Object> body = new HashMap<>();
        body.put("email", user.email());
        body.put("username", user.username());
        return body;
    }
}