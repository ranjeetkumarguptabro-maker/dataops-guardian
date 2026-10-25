
"""Unit tests for pre-upgrade validation."""

import unittest
from unittest.mock import patch

from dataguardian.preflight import run_preflight


class TestPreflight(unittest.TestCase):
    @patch("dataguardian.preflight.check_health")
    def test_all_checks_pass(self, mock_health):
        mock_health.return_value = {
            "status": "healthy",
            "error": None,
        }

        report = run_preflight("https://service.example/health")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["failed_count"], 0)
        mock_health.assert_called_once()

    @patch("dataguardian.preflight.check_health")
    def test_unhealthy_service_blocks_preflight(self, mock_health):
        mock_health.return_value = {
            "status": "unhealthy",
            "error": "Connection refused",
        }

        report = run_preflight("http://service.example/health")

        self.assertEqual(report["status"], "failed")
        self.assertGreater(report["failed_count"], 0)

    @patch("dataguardian.preflight.check_health")
    def test_invalid_url_skips_network_check(self, mock_health):
        report = run_preflight("not-a-valid-url")

        self.assertEqual(report["status"], "failed")
        mock_health.assert_not_called()

    @patch("dataguardian.preflight.check_health")
    def test_invalid_timeout_skips_network_check(self, mock_health):
        report = run_preflight(
            "https://service.example/health",
            timeout=0,
        )

        self.assertEqual(report["status"], "failed")
        mock_health.assert_not_called()

    @patch("dataguardian.preflight.check_health")
    def test_empty_required_version_fails(self, mock_health):
        report = run_preflight(
            "https://service.example/health",
            required_version="   ",
        )

        self.assertEqual(report["status"], "failed")
        mock_health.assert_not_called()

    @patch("dataguardian.preflight.check_health")
    @patch("dataguardian.preflight.get_elasticsearch_version")
    def test_discovered_matching_version_passes(
        self, mock_version, mock_health
    ):
        mock_version.return_value = {
            "status": "passed",
            "version": "8.15.0",
            "message": "Elasticsearch version retrieved successfully",
        }
        mock_health.return_value = {
            "status": "healthy",
            "error": None,
        }

        report = run_preflight(
            health_url="https://service.example/health",
            required_version="8.15.0",
            elasticsearch_url="https://elastic.example:9200",
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["failed_count"], 0)
        mock_version.assert_called_once_with(
            "https://elastic.example:9200",
            timeout=3.0,
        )

    @patch("dataguardian.preflight.check_health")
    @patch("dataguardian.preflight.get_elasticsearch_version")
    def test_discovered_mismatched_version_fails(
        self, mock_version, mock_health
    ):
        mock_version.return_value = {
            "status": "passed",
            "version": "8.12.0",
            "message": "Elasticsearch version retrieved successfully",
        }
        mock_health.return_value = {
            "status": "healthy",
            "error": None,
        }

        report = run_preflight(
            health_url="https://service.example/health",
            required_version="8.15.0",
            elasticsearch_url="https://elastic.example:9200",
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any(
                check["name"] == "version_check"
                and check["status"] == "failed"
                and check["message"] == "Version mismatch"
                for check in report["checks"]
            )
        )

    @patch("dataguardian.preflight.check_health")
    @patch("dataguardian.preflight.get_elasticsearch_version")
    def test_version_discovery_failure_fails_preflight(
        self, mock_version, mock_health
    ):
        mock_version.return_value = {
            "status": "failed",
            "version": None,
            "message": "Connection failed: connection refused",
        }
        mock_health.return_value = {
            "status": "healthy",
            "error": None,
        }

        report = run_preflight(
            health_url="https://service.example/health",
            required_version="8.15.0",
            elasticsearch_url="https://elastic.example:9200",
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any(
                check["name"] == "version_check"
                and check["status"] == "failed"
                for check in report["checks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
