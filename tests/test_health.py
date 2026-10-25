"""Unit tests for DataOps Guardian health checks."""

import unittest
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

from dataguardian.health import check_health


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

        self.assertEqual(
            result["target"],
            "https://service.example/health",
        )
        self.assertNotIn("secret", result["target"])
        self.assertNotIn("private", result["target"])

    def test_invalid_retry_count_returns_configuration_error(self):
        result = check_health(
            "https://service.example/health",
            retries=-1,
        )

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["error_type"], "configuration_error")


if __name__ == "__main__":
    unittest.main()
