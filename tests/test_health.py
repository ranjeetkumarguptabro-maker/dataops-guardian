
"""Unit tests for DataOps Guardian health checks."""

import unittest
from unittest.mock import MagicMock, patch
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

        result = check_health("http://service.example/health")

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["error_type"], "http_error")

    @patch("dataguardian.health.urlopen")
    def test_result_contains_required_fields(self, mock_urlopen):
        response = MagicMock()
        response.status = 200
        mock_urlopen.return_value.__enter__.return_value = response

        result = check_health("https://service.example/health")

        required_fields = {
            "service", "target", "status", "http_status",
            "duration_ms", "checked_at", "error_type", "error",
        }
        self.assertTrue(required_fields.issubset(result.keys()))


if __name__ == "__main__":
    unittest.main()
