
import unittest
from unittest.mock import patch

from dataguardian.preflight import (
    is_http_url,
    run_preflight,
)


class TestPreflight(unittest.TestCase):

    @patch("dataguardian.preflight.check_health")
    def test_all_checks_pass(self, mock_health):
        mock_health.return_value = {"status": "healthy"}

        report = run_preflight(
            health_url="https://example.com/health",
            required_version="8.15.0",
            actual_version="8.15.0",
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["failed_count"], 0)
        mock_health.assert_called_once()

    @patch("dataguardian.preflight.check_health")
    @patch("dataguardian.preflight.get_elasticsearch_version")
    def test_discovered_matching_version_passes(
        self, mock_version, mock_health
    ):
        mock_version.return_value = {
            "status": "passed",
            "version": "8.15.0",
            "message": "Version retrieved",
        }
        mock_health.return_value = {"status": "healthy"}

        report = run_preflight(
            health_url="https://example.com/health",
            required_version="8.15.0",
            elasticsearch_url="https://example.com:9200",
        )

        self.assertEqual(report["status"], "passed")
        mock_version.assert_called_once()

    @patch("dataguardian.preflight.check_health")
    @patch("dataguardian.preflight.get_elasticsearch_version")
    def test_discovered_mismatched_version_fails(
        self, mock_version, mock_health
    ):
        mock_version.return_value = {
            "status": "passed",
            "version": "8.14.0",
            "message": "Version retrieved",
        }
        mock_health.return_value = {"status": "healthy"}

        report = run_preflight(
            health_url="https://example.com/health",
            required_version="8.15.0",
            elasticsearch_url="https://example.com:9200",
        )

        self.assertEqual(report["status"], "failed")
        self.assertGreater(report["failed_count"], 0)

    def test_empty_required_version_fails(self):
        report = run_preflight(
            health_url="https://example.com/health",
            required_version=" ",
            actual_version="8.15.0",
        )

        self.assertEqual(report["status"], "failed")

    @patch("dataguardian.preflight.check_health")
    def test_invalid_timeout_skips_network_check(self, mock_health):
        report = run_preflight(
            health_url="https://example.com/health",
            timeout=0,
        )

        self.assertEqual(report["status"], "failed")
        mock_health.assert_not_called()

    @patch("dataguardian.preflight.check_health")
    def test_invalid_url_skips_network_check(self, mock_health):
        report = run_preflight(
            health_url="not-a-url",
        )

        self.assertEqual(report["status"], "failed")
        mock_health.assert_not_called()

    @patch("dataguardian.preflight.check_health")
    def test_unhealthy_service_blocks_preflight(self, mock_health):
        mock_health.return_value = {
            "status": "unhealthy",
            "error": "HTTP 503",
        }

        report = run_preflight(
            health_url="https://example.com/health",
        )

        self.assertEqual(report["status"], "failed")

    @patch("dataguardian.preflight.check_health")
    @patch("dataguardian.preflight.get_elasticsearch_version")
    def test_version_discovery_failure_fails_preflight(
        self, mock_version, mock_health
    ):
        mock_version.return_value = {
            "status": "failed",
            "version": None,
            "message": "Could not connect to Elasticsearch",
        }
        mock_health.return_value = {"status": "healthy"}

        report = run_preflight(
            health_url="https://example.com/health",
            required_version="8.15.0",
            elasticsearch_url="https://example.com:9200",
        )

        self.assertEqual(report["status"], "failed")

    # Security tests

    def test_url_with_embedded_credentials_is_rejected(self):
        self.assertFalse(
            is_http_url("https://admin:secret@example.com/health")
        )

    def test_url_with_fragment_is_rejected(self):
        self.assertFalse(
            is_http_url("https://example.com/health#section")
        )

    def test_valid_https_url_is_accepted(self):
        self.assertTrue(
            is_http_url("https://example.com/health")
        )


if __name__ == "__main__":
    unittest.main()
