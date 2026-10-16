package com.oncoord.propertyscout.auth;

import com.oncoord.accounts.auth.AccountNotFoundException;
import com.oncoord.accounts.auth.AuthService;
import com.oncoord.auth.common.RecaptchaClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.mail.MailException;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * Thin HTTP layer over oncoord-accounts' AuthService. Errors come back as
 * { "error": "..." }, which register.html / post-login.html display as-is.
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private static final Logger log = LoggerFactory.getLogger(AuthController.class);

    /** username is sent only from register.html's "Create account" mode; null on "Sign in". */
    public record RegisterRequest(String email, String username, String captcha) {}
    public record VerifyRequest(String token) {}

    private final AuthService auth;
    private final RecaptchaClient recaptcha;

    public AuthController(AuthService auth, RecaptchaClient recaptcha) {
        this.auth = auth;
        this.recaptcha = recaptcha;
    }

    /**
     * register.html posts here in both modes:
     *
     *   "Sign in" (no username)  existing accounts only. An unknown email gets
     *                            a 404 with code "account_not_found", and the
     *                            page switches to "Create account".
     *   "Create account"         creates the account with its name if new; an
     *                            existing account's name is never changed.
     */
    @PostMapping("/register")
    public ResponseEntity<Map<String, String>> register(@RequestBody RegisterRequest request) {
        if (request == null || request.captcha() == null || !recaptcha.verify(request.captcha())) {
            return error(HttpStatus.BAD_REQUEST, "The CAPTCHA check failed. Complete it again, then resend.");
        }
        boolean createAccount = request.username() != null;
        String username = null;
        if (createAccount) {
            try {
                username = AuthService.normalizeUsername(request.username());
            } catch (IllegalArgumentException e) {
                return error(HttpStatus.BAD_REQUEST,
                        "Enter a name of up to " + AuthService.MAX_USERNAME_LENGTH + " characters.");
            }
        }
        try {
            if (createAccount) {
                auth.requestLoginLink(request.email(), username);
            } else {
                auth.requestSignInLink(request.email());
            }
        } catch (AccountNotFoundException e) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND).body(Map.of(
                    "code", "account_not_found",
                    "error", "We don't have an account for that email yet. Add your name to create one."));
        } catch (IllegalArgumentException e) {
            return error(HttpStatus.BAD_REQUEST, "Enter a valid email address.");
        } catch (MailException e) {
            log.error("Sign-in email could not be sent", e);
            return error(HttpStatus.SERVICE_UNAVAILABLE,
                    "The sign-in email could not be sent. Try again, or contact PropertyScout@oncoord.com.");
        }
        return ResponseEntity.ok(Map.of("status", "sent"));
    }

    /** post-login.html: trades the emailed one-time token for a session token. */
    @PostMapping("/verify")
    public ResponseEntity<Map<String, String>> verify(@RequestBody VerifyRequest request) {
        String token = request == null ? null : request.token();
        return auth.verify(token)
                .map(session -> ResponseEntity.ok(Map.of(
                        "sessionToken", session.sessionToken(),
                        "email", session.user().email())))
                .orElseGet(() -> error(HttpStatus.UNAUTHORIZED,
                        "This sign-in link has already been used or has expired. Request a new one."));
    }

    /** Ends the caller's session. Always 204, even if the token was already invalid. */
    @PostMapping("/logout")
    public ResponseEntity<Void> logout(@RequestHeader(value = "X-Session-Token", required = false) String sessionToken) {
        auth.logout(sessionToken);
        return ResponseEntity.noContent().build();
    }

    private static ResponseEntity<Map<String, String>> error(HttpStatus status, String message) {
        return ResponseEntity.status(status).body(Map.of("error", message));
    }
}