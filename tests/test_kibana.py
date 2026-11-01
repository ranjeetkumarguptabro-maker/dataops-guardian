import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from dataguardian.kibana import DEFAULT_FILE, import_dashboard


def kibana_response(mock_urlopen, payload):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    mock_urlopen.return_value.__enter__.return_value = response


class ImportDashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "objects.ndjson"
        self.path.write_text('{"type":"dashboard"}\n')

    @patch("dataguardian.kibana.urlopen")
    def test_upload_format(self, mock_urlopen):
        kibana_response(mock_urlopen, {"success": True, "successCount": 2})

        with patch.dict(os.environ, {"ELASTICSEARCH_API_KEY": "test-key"}):
            result = import_dashboard("https://kibana.example/", str(self.path))

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(result["status"], "passed")
        self.assertIn("/api/saved_objects/_import?overwrite=true", request.full_url)
        self.assertEqual(request.get_header("Kbn-xsrf"), "true")
        self.assertEqual(request.get_header("Authorization"), "ApiKey test-key")
        self.assertIn(b'{"type":"dashboard"}', request.data)

    @patch("dataguardian.kibana.urlopen")
    def test_partial_import_fails(self, mock_urlopen):
        kibana_response(mock_urlopen, {"success": False, "errors": [{"id": "x"}]})

        result = import_dashboard("https://kibana.example", str(self.path))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], [{"id": "x"}])

    @patch("dataguardian.kibana.urlopen")
    def test_connection_problems_fail(self, mock_urlopen):
        for error in (
            URLError("refused"),
            HTTPError("u", 403, "Forbidden", {}, None),
            HTTPError("u", 500, "Error", {}, None),
        ):
            with self.subTest(error=error):
                mock_urlopen.side_effect = error
                result = import_dashboard("https://kibana.example", str(self.path))
                self.assertEqual(result["status"], "failed")

    def test_missing_file_fails(self):
        result = import_dashboard("https://kibana.example", "/does/not/exist")
        self.assertEqual(result["status"], "failed")


class DashboardFileTests(unittest.TestCase):
    def test_every_reference_resolves(self):
        lines = Path(DEFAULT_FILE).read_text().splitlines()
        objects = [json.loads(line) for line in lines]
        available = {(item["type"], item["id"]) for item in objects}

        self.assertIn(("dashboard", "guardian-overview"), available)
        for item in objects:
            for reference in item.get("references", []):
                self.assertIn((reference["type"], reference["id"]), available)


if __name__ == "__main__":
    unittest.main()
