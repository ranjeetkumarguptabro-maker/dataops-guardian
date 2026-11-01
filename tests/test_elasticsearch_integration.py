"""Optional integration tests against a live Elasticsearch instance."""

import os
import unittest

from dataguardian.elasticsearch import get_elasticsearch_version
from dataguardian.upgrade_checks import run_upgrade_checks


@unittest.skipUnless(
    os.getenv("ELASTICSEARCH_INTEGRATION_TESTS") == "1",
    "Set ELASTICSEARCH_INTEGRATION_TESTS=1 to run live integration tests",
)
class TestElasticsearchIntegration(unittest.TestCase):
    def test_discovers_live_elasticsearch_version(self):
        url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
        expected_version = os.getenv("ELASTICSEARCH_EXPECTED_VERSION")

        result = get_elasticsearch_version(url)

        self.assertEqual(result["status"], "passed", msg=result["message"])
        self.assertTrue(result["version"])

        if expected_version:
            self.assertEqual(result["version"], expected_version)

    def test_upgrade_checks_run_against_live_cluster(self):
        url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
        major, minor, _ = get_elasticsearch_version(url)["version"].split(".")

        report = run_upgrade_checks(url, f"{major}.{int(minor) + 1}.0")

        self.assertNotEqual(report["status"], "blocked", msg=report["checks"])
        self.assertIs(report["changes_made"], False)
