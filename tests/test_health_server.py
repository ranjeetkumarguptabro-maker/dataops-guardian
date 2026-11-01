import json
import os
import signal
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from dataguardian.health_server import HealthHandler


class HealthServerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_health_returns_healthy_json(self):
        with patch.dict("os.environ", {"SERVICE_NAME": "guardian-test"}):
            with urlopen(f"{self.base_url}/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "application/json")
                body = json.load(response)

        self.assertEqual(body, {"service": "guardian-test", "status": "healthy"})

    def test_unknown_path_returns_404(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base_url}/unknown", timeout=5)

        self.addCleanup(caught.exception.close)
        self.assertEqual(caught.exception.code, 404)


class HealthServerShutdownTests(unittest.TestCase):
    def test_sigterm_stops_server_cleanly(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "dataguardian.health_server"],
            env={**os.environ, "PORT": "0", "HEALTH_HOST": "127.0.0.1"},
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.kill)

        self.assertIn("listening", process.stderr.readline())
        process.send_signal(signal.SIGTERM)

        self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
