import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from dataguardian.splunk import check_splunk_health, publish_report, send_event

URL = "https://splunk.example:8088"


class SplunkTests(unittest.TestCase):
    @patch("dataguardian.splunk.urlopen")
    def test_send_event_uses_hec_format_and_token(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = MagicMock()

        result = send_event(URL, "test-token", {"status": "ready"})

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(request.full_url, f"{URL}/services/collector/event")
        self.assertEqual(request.get_header("Authorization"), "Splunk test-token")
        self.assertEqual(json.loads(request.data)["event"], {"status": "ready"})

    @patch("dataguardian.splunk.urlopen")
    def test_rejected_token_is_reported_without_the_token(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(URL, 403, "Forbidden", {}, None)

        result = send_event(URL, "secret-value", {})

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret-value", json.dumps(result))

    @patch("dataguardian.splunk.urlopen")
    def test_health_check(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = MagicMock()
        self.assertEqual(check_splunk_health(URL)["status"], "passed")

        mock_urlopen.side_effect = URLError("refused")
        result = check_splunk_health(URL)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["name"], "splunk_hec")

    def test_invalid_url_fails_without_a_request(self):
        self.assertEqual(send_event("ftp://splunk", "t", {})["status"], "failed")

    def test_publish_is_skipped_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(publish_report({})["status"], "skipped")

    @patch("dataguardian.splunk.urlopen", side_effect=URLError("refused"))
    def test_publish_outage_is_returned_not_raised(self, _mock_urlopen):
        env = {"SPLUNK_HEC_URL": URL, "SPLUNK_HEC_TOKEN": "t"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(publish_report({})["status"], "failed")


if __name__ == "__main__":
    unittest.main()
