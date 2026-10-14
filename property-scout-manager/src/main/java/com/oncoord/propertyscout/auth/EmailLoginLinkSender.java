package com.oncoord.propertyscout.auth;

import com.oncoord.accounts.auth.LoginLinkSender;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.mail.SimpleMailMessage;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.stereotype.Component;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

@Component
public class EmailLoginLinkSender implements LoginLinkSender {

    private final JavaMailSender mailSender;
    private final String mailFrom;
    private final String appBaseUrl;
    private final long linkTtlMinutes;

    public EmailLoginLinkSender(JavaMailSender mailSender,
                                @Value("${propertyscout.mail-from}") String mailFrom,
                                @Value("${propertyscout.app-base-url}") String appBaseUrl,
                                @Value("${oncoord.accounts.login-link-ttl-minutes:30}") long linkTtlMinutes) {
        this.mailSender = mailSender;
        this.mailFrom = mailFrom;
        this.appBaseUrl = appBaseUrl.replaceAll("/+$", "");
        this.linkTtlMinutes = linkTtlMinutes;
    }

    @Override
    public void send(String email, String loginToken) {
        // Fragment (#), not query string (?): the browser never sends the
        // fragment to any server, so the token stays out of access logs and
        // Referer headers. Same approach as AuctionScout's links.
        String link = appBaseUrl + "/post-login.html#token="
                + URLEncoder.encode(loginToken, StandardCharsets.UTF_8);

        SimpleMailMessage message = new SimpleMailMessage();
        message.setFrom(mailFrom);
        message.setTo(email);
        message.setSubject("Your PropertyScout sign-in link");
        message.setText("""
                Use this link to sign in to PropertyScout:

                %s

                The link works once and expires in %d minutes.
                If you didn't ask to sign in, you can ignore this email.

                PropertyScout is an OnCoord product.
                """.formatted(link, linkTtlMinutes));
        mailSender.send(message);
    }
}