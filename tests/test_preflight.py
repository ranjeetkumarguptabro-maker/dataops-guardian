
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


if __name__ == "__main__":
    unittest.main()
