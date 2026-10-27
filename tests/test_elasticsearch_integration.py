"""Optional integration tests against a live Elasticsearch instance."""

import os
import unittest

from dataguardian.elasticsearch import get_elasticsearch_version


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
