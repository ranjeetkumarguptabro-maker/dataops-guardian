import subprocess
import unittest
from unittest.mock import patch

from dataguardian.elasticsearch import ElasticsearchError
from dataguardian.platform_status import (
    collect_status,
    index_report,
    summarize_pods,
)


def responses(cpu="10", heap="40", disk="50", status="green"):
    return {
        "/_cluster/health": {"status": status},
        "/_cat/nodes?format=json&h=name,version,cpu,ram.percent,heap.percent": [
            {
                "name": "es-0",
                "version": "9.5.5",
                "cpu": cpu,
                "ram.percent": "60",
                "heap.percent": heap,
            }
        ],
        "/_cat/allocation?format=json&h=node,disk.percent": [
            {"node": "es-0", "disk.percent": disk},
            {"node": "UNASSIGNED", "disk.percent": None},
        ],
    }


def pod(name="es-0", ready="True", restarts=0):
    return {
        "metadata": {"name": name},
        "status": {
            "conditions": [{"type": "Ready", "status": ready}],
            "containerStatuses": [{"restartCount": restarts}],
        },
    }


class CollectStatusTests(unittest.TestCase):
    def collect(self, data):
        def fake(_url, path, _timeout):
            return data[path]

        with patch("dataguardian.platform_status.fetch_json", fake):
            return collect_status("http://es")

    def test_healthy_cluster_reports_resources(self):
        report = self.collect(responses())

        self.assertEqual(report["status"], "healthy")
        node = report["elasticsearch"]["nodes"][0]
        self.assertEqual(node["cpu_percent"], 10)
        self.assertEqual(node["ram_percent"], 60)
        self.assertEqual(node["disk_percent"], 50)

    def test_thresholds_raise_alerts(self):
        cases = [
            ({"cpu": "90"}, "CPU"),
            ({"heap": "75"}, "heap"),
            ({"disk": "75"}, "disk"),
            ({"status": "yellow"}, "health"),
        ]
        for options, word in cases:
            with self.subTest(options=options):
                report = self.collect(responses(**options))
                self.assertEqual(report["status"], "alert")
                self.assertIn(word, report["alerts"][0])

    def test_high_ram_alone_does_not_alert(self):
        data = responses()
        path = "/_cat/nodes?format=json&h=name,version,cpu,ram.percent,heap.percent"
        data[path][0]["ram.percent"] = "99"

        self.assertEqual(self.collect(data)["status"], "healthy")

    def test_unreachable_elasticsearch_is_an_alert(self):
        error = ElasticsearchError("Could not connect")
        with patch("dataguardian.platform_status.fetch_json", side_effect=error):
            report = collect_status("http://es")

        self.assertEqual(report["status"], "alert")
        self.assertIn("Could not connect", report["alerts"][0])

    @patch("dataguardian.platform_status.fetch_json", side_effect=KeyError("x"))
    def test_kubectl_failure_is_an_alert(self, _fetch):
        failure = subprocess.CalledProcessError(1, "kubectl")
        with patch("subprocess.run", side_effect=failure):
            report = collect_status("http://es", namespace="elasticsearch")

        self.assertIn("Kubernetes pod status unavailable", report["alerts"])


class SummarizePodsTests(unittest.TestCase):
    def test_healthy_pod(self):
        pods, alerts = summarize_pods({"items": [pod()]})

        self.assertEqual(pods, [{"name": "es-0", "ready": True, "restarts": 0}])
        self.assertEqual(alerts, [])

    def test_unready_and_restarting_pods_alert(self):
        _, alerts = summarize_pods(
            {"items": [pod("a", ready="False"), pod("b", restarts=3)]}
        )

        self.assertEqual(len(alerts), 2)


class IndexReportTests(unittest.TestCase):
    def test_report_gets_a_timestamp_and_failure_is_returned(self):
        with patch("dataguardian.platform_status.request_json") as mocked:
            result = index_report("http://es", {"generated_at": "now", "status": "ok"})

        self.assertEqual(result["status"], "passed")
        self.assertEqual(mocked.call_args.kwargs["body"]["@timestamp"], "now")

        with patch(
            "dataguardian.platform_status.request_json",
            side_effect=ElasticsearchError("HTTP 403"),
        ):
            result = index_report("http://es", {"generated_at": "now"})

        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
