
import base64
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from dataguardian.elasticsearch import get_elasticsearch_version


class TestElasticsearchVersion(unittest.TestCase):
    @patch("dataguardian.elasticsearch.urlopen")
    def test_retrieves_version(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "version": {"number": "8.15.0"}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        result = get_elasticsearch_version("https://localhost:9200")

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["version"], "8.15.0")

    @patch("dataguardian.elasticsearch.urlopen")
    def test_missing_version_fails(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = b'{"name":"test-node"}'
        mock_urlopen.return_value.__enter__.return_value = response

        result = get_elasticsearch_version("https://localhost:9200")

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["version"])

    @patch("dataguardian.elasticsearch.urlopen")
    def test_basic_auth_header_is_sent(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "version": {"number": "8.15.0"}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        result = get_elasticsearch_version(
            "https://localhost:9200",
            username="test-user",
            password="test-password",
        )

        request = mock_urlopen.call_args.args[0]
        expected = base64.b64encode(
            b"test-user:test-password"
        ).decode("ascii")

        self.assertEqual(
            request.get_header("Authorization"),
            f"Basic {expected}",
        )
        self.assertEqual(result["status"], "passed")

    def test_missing_password_fails_without_request(self):
        with patch.dict(
            os.environ,
            {"ELASTICSEARCH_USERNAME": "test-user"},
            clear=True,
        ):
            with patch(
                "dataguardian.elasticsearch.urlopen"
            ) as mock_urlopen:
                result = get_elasticsearch_version(
                    "https://localhost:9200"
                )

        self.assertEqual(result["status"], "failed")
        mock_urlopen.assert_not_called()

    @patch("dataguardian.elasticsearch.urlopen")
    def test_unauthorized_response_is_safe(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://localhost:9200/",
            401,
            "Unauthorized",
            {},
            None,
        )

        result = get_elasticsearch_version("https://localhost:9200")

        self.assertEqual(result["status"], "failed")
        self.assertIn("authentication", result["message"].lower())
        self.assertNotIn("password", result["message"].lower())

    @patch(
        "dataguardian.elasticsearch.urlopen",
        side_effect=URLError("connection refused"),
    )
    def test_connection_failure(self, mock_urlopen):
        result = get_elasticsearch_version("https://localhost:9200")

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["version"])


if __name__ == "__main__":
    unittest.main()
