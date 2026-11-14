import json
import unittest
from unittest.mock import Mock

import requests

from supysonic.metadata_provider_errors import (
    safe_metadata_error_details, transport_error_details,
)


class MetadataProviderErrorsTestCase(unittest.TestCase):
    def response(self, status, *, body=None, headers=None):
        response = Mock(status_code=status, headers=headers or {}, text="", reason="")
        response.json.return_value = body or {}
        return response

    def test_only_explicit_transient_http_statuses_are_retryable(self):
        for status in (408, 429, 500, 502, 503, 599):
            with self.subTest(status=status):
                details = safe_metadata_error_details(self.response(status))
                self.assertTrue(details["retryable"])
                self.assertEqual(details["upstreamStatus"], status)
                self.assertEqual(details["rateLimited"], status == 429)
        for status in (200, 302, 400, 401, 403, 404, 422, 600, None, True, "503"):
            with self.subTest(status=status):
                details = safe_metadata_error_details(self.response(status))
                self.assertFalse(details["retryable"])
                if type(status) is not int or not 100 <= status <= 599:
                    self.assertNotIn("upstreamStatus", details)

    def test_quota_recognition_overrides_retryable_status(self):
        for status, body in (
            (402, {}),
            (429, {"error": {"code": "insufficient_quota"}}),
            (503, {"error": {"message": "Your billing quota exceeded"}}),
        ):
            with self.subTest(status=status, body=body):
                details = safe_metadata_error_details(self.response(status, body=body))
                self.assertTrue(details["quotaExhausted"])
                self.assertFalse(details["retryable"])
                self.assertFalse(details["rateLimited"])

    def test_retry_after_is_bounded_finite_and_only_for_transient_failure(self):
        for status in (429, 503):
            for value, expected in (("15", 15), ("1.5", 1.5), ("9999999", 86400)):
                with self.subTest(status=status, value=value):
                    details = safe_metadata_error_details(self.response(
                        status, headers={"Retry-After": value},
                    ))
                    self.assertEqual(details["retryAfterSeconds"], expected)
            for value in ("nan", "inf", "-inf", "-1", "0", "invalid"):
                with self.subTest(status=status, value=value):
                    details = safe_metadata_error_details(self.response(
                        status, headers={"Retry-After": value},
                    ))
                    self.assertNotIn("retryAfterSeconds", details)
        self.assertNotIn("retryAfterSeconds", safe_metadata_error_details(
            self.response(401, headers={"Retry-After": "30"}),
        ))

    def test_lowercase_retry_header_is_supported(self):
        details = safe_metadata_error_details(self.response(429, headers={"retry-after": "90"}))
        self.assertEqual(details["retryAfterSeconds"], 90)

    def test_body_text_codes_and_credentials_never_leave_allowlist(self):
        secret = "private-secret"
        response = self.response(503, body={"error": {
            "message": "Bearer " + secret + " https://private.invalid/token",
            "code": "internal-" + secret,
        }}, headers={"Authorization": "Bearer " + secret, "Retry-After": "12"})
        details = safe_metadata_error_details(response, api_key=secret)
        self.assertEqual(details, {
            "upstreamStatus": 503, "retryable": True, "quotaExhausted": False,
            "rateLimited": False, "retryAfterSeconds": 12,
        })
        self.assertNotIn(secret, json.dumps(details))
        self.assertNotIn("private.invalid", json.dumps(details))

    def test_non_json_error_response_is_classified_without_retaining_body(self):
        response = self.response(502)
        response.json.side_effect = ValueError("private-body")
        response.text = "private-body"
        details = safe_metadata_error_details(response)
        self.assertTrue(details["retryable"])
        self.assertNotIn("private-body", json.dumps(details))

    def test_only_explicit_transient_transport_errors_are_retryable(self):
        for error_type, category in (
            (requests.exceptions.Timeout, "timeout"),
            (requests.exceptions.ConnectTimeout, "timeout"),
            (requests.exceptions.ReadTimeout, "timeout"),
            (requests.exceptions.ConnectionError, "network"),
            (requests.exceptions.ChunkedEncodingError, "network"),
        ):
            with self.subTest(error_type=error_type):
                details = transport_error_details(error_type("secret-url-and-key"))
                self.assertEqual(details, {"retryable": True, "transportError": category})
        for error_type in (requests.exceptions.SSLError, requests.exceptions.InvalidURL,
                           requests.exceptions.InvalidSchema, requests.exceptions.RequestException):
            with self.subTest(error_type=error_type):
                self.assertEqual(transport_error_details(error_type("secret-url-and-key")),
                                 {"retryable": False})
