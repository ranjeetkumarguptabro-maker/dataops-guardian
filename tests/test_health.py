"""Unit tests for DataOps Guardian health checks."""

import json
import os
import unittest
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

from dataguardian.health import _sanitize_url, check_health, main


class TestSanitizeURL(unittest.TestCase):
    def test_rejects_invalid_scheme(self):
        self.assertEqual(_sanitize_url("ftp://service.example"), "<invalid URL>")

    def test_rejects_missing_hostname(self):
        self.assertEqual(_sanitize_url("https:///health"), "<invalid URL>")

    def test_removes_credentials_query_and_fragment(self):
        result = _sanitize_url(
            "https://user:secret@service.example/health?token=private#section"
        )
        self.assertEqual(result, "https://service.example/health")
        self.assertNotIn("secret", result)
        self.assertNotIn("private", result)

    def test_preserves_port(self):
        self.assertEqual(
            _sanitize_url("http://service.example:8080/health"),
            "http://service.example:8080/health",
        )

    def test_brackets_ipv6_hostname(self):
        self.assertEqual(
            _sanitize_url("http://[::1]:8080/health"),
            "http://[::1]:8080/health",
        )

    def test_malformed_port_is_invalid(self):
        self.assertEqual(
            _sanitize_url("http://service.example:bad/health"),
            "<invalid URL>",
        )


class TestHealthCheck(unittest.TestCase):
    @patch("dataguardian.health.urlopen")
    def test_successful_response(self, mock_urlopen):
        response = MagicMock()
        response.status = 200
        mock_urlopen.return_value.__enter__.return_value = response

        result = check_health("https://service.example/health")

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["http_status"], 200)

    @patch("dataguardian.health.urlopen")
    def test_connection_failure(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("connection refused")

        result = check_health("http://service.example/health")

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["error_type"], "connection_error")
        mock_urlopen.assert_called_once()

    @patch("dataguardian.health.urlopen")
    def test_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("request timed out")

        result = check_health("http://service.example/health")

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["error_type"], "timeout")

    @patch("dataguardian.health.urlopen")
    def test_http_503_is_unhealthy(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "http://service.example/health",
            503,
            "Service Unavailable",
            None,
            None,
        )

        result = check_health(
            "http://service.example/health",
            retries=3,
        )

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["error_type"], "http_error")
        mock_urlopen.assert_called_once()

    @patch("dataguardian.health.urlopen")
    def test_result_contains_required_fields(self, mock_urlopen):
        response = MagicMock()
        response.status = 200
        mock_urlopen.return_value.__enter__.return_value = response

        result = check_health("https://service.example/health")

        required_fields = {
            "service",
            "target",
            "status",
            "http_status",
            "duration_ms",
            "checked_at",
            "error_type",
            "error",
        }
        self.assertTrue(required_fields.issubset(result.keys()))

    @patch("dataguardian.health.time.sleep")
    @patch("dataguardian.health.urlopen")
    def test_retries_connection_failure_then_succeeds(self, mock_urlopen, mock_sleep):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        mock_urlopen.side_effect = [
            URLError("connection refused"),
            response,
        ]

        result = check_health(
            "https://service.example/health",
            retries=2,
            retry_backoff=0.25,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(0.25)

    @patch("dataguardian.health.time.sleep")
    @patch("dataguardian.health.urlopen")
    def test_retries_exhausted(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = URLError("connection refused")

        result = check_health(
            "https://service.example/health",
            retries=2,
            retry_backoff=0.25,
        )

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["error_type"], "connection_error")
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(
            mock_sleep.call_args_list,
            [call(0.25), call(0.5)],
        )

    @patch("dataguardian.health.time.sleep")
    @patch("dataguardian.health.urlopen")
    def test_http_error_is_not_retried(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = HTTPError(
            "https://service.example/health",
            401,
            "Unauthorized",
            None,
            None,
        )

        result = check_health(
            "https://service.example/health",
            retries=3,
        )

        self.assertEqual(result["error_type"], "http_error")
        self.assertEqual(result["http_status"], 401)
        mock_urlopen.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("dataguardian.health.urlopen")
    def test_target_url_does_not_expose_query_or_credentials(self, mock_urlopen):
        response = MagicMock()
        response.status = 200
        mock_urlopen.return_value.__enter__.return_value = response

        result = check_health(
            "https://user:secret@service.example/health?token=private#section"
        )

        self.assertEqual(result["target"], "https://service.example/health")
        self.assertNotIn("secret", result["target"])
        self.assertNotIn("private", result["target"])

    def test_invalid_retry_count_returns_configuration_error(self):
        result = check_health(
            "https://service.example/health",
            retries=-1,
        )

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["error_type"], "configuration_error")

    def test_boolean_timeout_is_rejected(self):
        result = check_health("https://service.example/health", timeout=True)
        self.assertEqual(result["error_type"], "configuration_error")

    def test_non_numeric_timeout_is_rejected(self):
        result = check_health("https://service.example/health", timeout="3")
        self.assertEqual(result["error_type"], "configuration_error")

    def test_non_finite_timeout_is_rejected(self):
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout):
                result = check_health(
                    "https://service.example/health",
                    timeout=timeout,
                )
                self.assertEqual(result["error_type"], "configuration_error")

    def test_timeout_above_maximum_is_rejected(self):
        result = check_health("https://service.example/health", timeout=61)
        self.assertEqual(result["error_type"], "configuration_error")

    def test_boolean_retry_count_is_rejected(self):
        result = check_health("https://service.example/health", retries=True)
        self.assertEqual(result["error_type"], "configuration_error")

    def test_non_integer_retry_count_is_rejected(self):
        result = check_health("https://service.example/health", retries=1.5)
        self.assertEqual(result["error_type"], "configuration_error")

    def test_boolean_retry_backoff_is_rejected(self):
        result = check_health(
            "https://service.example/health",
            retry_backoff=True,
        )
        self.assertEqual(result["error_type"], "configuration_error")

    def test_negative_retry_backoff_is_rejected(self):
        result = check_health(
            "https://service.example/health",
            retry_backoff=-0.1,
        )
        self.assertEqual(result["error_type"], "configuration_error")

    def test_non_finite_retry_backoff_is_rejected(self):
        for delay in (float("nan"), float("inf")):
            with self.subTest(delay=delay):
                result = check_health(
                    "https://service.example/health",
                    retry_backoff=delay,
                )
                self.assertEqual(result["error_type"], "configuration_error")

    @patch("dataguardian.health.urlopen")
    def test_unexpected_success_response_status_is_unhealthy(self, mock_urlopen):
        response = MagicMock()
        response.status = 302
        mock_urlopen.return_value.__enter__.return_value = response

        result = check_health("https://service.example/health")

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["error_type"], "http_error")
        self.assertEqual(result["http_status"], 302)

    @patch("dataguardian.health.urlopen")
    def test_tls_error_is_not_retried(self, mock_urlopen):
        import ssl

        mock_urlopen.side_effect = URLError(ssl.SSLError("TLS failure"))

        result = check_health(
            "https://service.example/health",
            retries=2,
        )

        self.assertEqual(result["error_type"], "tls_error")
        mock_urlopen.assert_called_once()

    @patch("dataguardian.health.urlopen")
    @patch("dataguardian.health.time.sleep")
    def test_url_timeout_error_can_be_retried(self, mock_sleep, mock_urlopen):
        mock_urlopen.side_effect = [
            URLError(TimeoutError("timed out")),
            MagicMock(
                status=200,
                __enter__=MagicMock(return_value=MagicMock(status=200)),
            ),
        ]

        result = check_health(
            "https://service.example/health",
            retries=1,
            retry_backoff=0,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(0.0)

    @patch("dataguardian.health.urlopen")
    def test_os_error_is_reported_as_request_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("socket failure")

        result = check_health("https://service.example/health")

        self.assertEqual(result["error_type"], "request_error")
        self.assertEqual(result["error"], "Network request failed")

    @patch("dataguardian.health.urlopen")
    def test_value_error_is_reported_as_invalid_request_url(self, mock_urlopen):
        mock_urlopen.side_effect = ValueError("bad URL")

        result = check_health(
            "https://service.example/health",
            retries=2,
        )

        self.assertEqual(result["error_type"], "request_error")
        self.assertEqual(result["error"], "Invalid request URL")
        mock_urlopen.assert_called_once()


class TestHealthCLI(unittest.TestCase):
    @patch("dataguardian.health.check_health")
    def test_main_healthy_result_returns_zero(self, mock_check):
        mock_check.return_value = {"status": "healthy"}
        with (
            patch(
                "sys.argv",
                ["dataguardian-health", "--url", "https://service.example/health"],
            ),
            patch("builtins.print") as mock_print,
        ):
            result = main()

        self.assertEqual(result, 0)
        mock_print.assert_called_once_with(json.dumps({"status": "healthy"}, indent=2))

    @patch("dataguardian.health.check_health")
    def test_main_unhealthy_result_returns_one(self, mock_check):
        mock_check.return_value = {"status": "unhealthy"}
        with (
            patch(
                "sys.argv",
                ["dataguardian-health", "--url", "https://service.example/health"],
            ),
            patch("builtins.print") as mock_print,
        ):
            result = main()

        self.assertEqual(result, 1)
        mock_print.assert_called_once_with(
            json.dumps({"status": "unhealthy"}, indent=2)
        )

    def test_main_rejects_missing_url(self):
        with patch.dict(os.environ, {"TARGET_HEALTH_URL": ""}):
            with patch("sys.argv", ["dataguardian-health"]):
                with self.assertRaises(SystemExit) as exc_info:
                    main()
        self.assertEqual(exc_info.exception.code, 2)

    def test_main_rejects_invalid_url(self):
        with patch(
            "sys.argv",
            ["dataguardian-health", "--url", "ftp://service.example"],
        ):
            with self.assertRaises(SystemExit) as exc_info:
                main()
        self.assertEqual(exc_info.exception.code, 2)

    def test_main_rejects_invalid_timeout(self):
        with patch(
            "sys.argv",
            [
                "dataguardian-health",
                "--url",
                "https://service.example/health",
                "--timeout",
                "0",
            ],
        ):
            with self.assertRaises(SystemExit) as exc_info:
                main()
        self.assertEqual(exc_info.exception.code, 2)

    def test_main_rejects_negative_retries(self):
        with patch(
            "sys.argv",
            [
                "dataguardian-health",
                "--url",
                "https://service.example/health",
                "--retries",
                "-1",
            ],
        ):
            with self.assertRaises(SystemExit) as exc_info:
                main()
        self.assertEqual(exc_info.exception.code, 2)

    def test_main_rejects_invalid_retry_backoff(self):
        with patch(
            "sys.argv",
            [
                "dataguardian-health",
                "--url",
                "https://service.example/health",
                "--retry-backoff",
                "-1",
            ],
        ):
            with self.assertRaises(SystemExit) as exc_info:
                main()
        self.assertEqual(exc_info.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
