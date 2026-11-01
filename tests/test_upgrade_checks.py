import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataguardian.elasticsearch import ElasticsearchError
from dataguardian.upgrade_checks import (
    check_cluster_health,
    check_deprecations,
    check_disk,
    check_nodes,
    check_shard_allocation,
    check_shards,
    check_snapshot,
    check_upgrade_path,
    run_upgrade_checks,
)
from dataguardian.upgrade_cli import main

NOW = 1_800_000_000


def healthy_responses() -> dict:
    return {
        "/": {"version": {"number": "8.17.4"}},
        "/_cluster/health": {
            "status": "green",
            "number_of_nodes": 3,
            "relocating_shards": 0,
            "initializing_shards": 0,
        },
        "/_cluster/settings?flat_settings=true": {"persistent": {}, "transient": {}},
        "/_cat/nodes?format=json&h=name,version,heap.percent": [
            {"name": f"node-{i}", "version": "8.17.4", "heap.percent": "40"}
            for i in range(3)
        ],
        "/_cat/allocation?format=json&h=node,disk.percent": [
            {"node": "node-0", "disk.percent": "40"},
            {"node": "UNASSIGNED", "disk.percent": None},
        ],
        "/_migration/deprecations": {
            "cluster_settings": [],
            "index_settings": {},
        },
        "/_cat/snapshots?format=json&h=id,status,end_epoch": [
            {"id": "nightly", "status": "SUCCESS", "end_epoch": "9999999999"},
        ],
    }


class TestUpgradePath(unittest.TestCase):
    def test_paths(self):
        cases = [
            ("8.17.4", "8.19.0", "passed"),
            ("8.17.4", "8.17.5", "passed"),
            ("7.17.20", "8.19.0", "passed"),
            ("8.18.2", "9.0.1", "passed"),
            ("8.19.3", "9.0.1", "passed"),
            ("8.19.3", "9.5.5", "passed"),
            ("8.18.2", "9.1.0", "failed"),
            ("8.17.4", "9.0.0", "failed"),
            ("7.16.0", "8.19.0", "failed"),
            ("7.17.20", "9.5.5", "failed"),
            ("8.17.4", "10.0.0", "failed"),
            ("8.17.4", "8.17.4", "failed"),
            ("8.17.4", "8.16.0", "failed"),
            ("8.17.4", "latest", "failed"),
            ("8.17.4", "9.0.0-rc1", "failed"),
        ]
        for current, target, expected in cases:
            with self.subTest(current=current, target=target):
                result = check_upgrade_path(current, target)
                self.assertEqual(result["status"], expected)


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.responses = healthy_responses()

    def fetch(self, path):
        return self.responses[path]

    def test_cluster_health(self):
        cases = [
            ("green", 3, "passed"),
            ("yellow", 1, "warning"),
            ("yellow", 3, "failed"),
            ("red", 1, "failed"),
        ]
        for status, nodes, expected in cases:
            with self.subTest(status=status, nodes=nodes):
                health = self.responses["/_cluster/health"]
                health.update(status=status, number_of_nodes=nodes)
                self.assertEqual(check_cluster_health(self.fetch)["status"], expected)

    def test_moving_shards_fail(self):
        self.responses["/_cluster/health"]["relocating_shards"] = 2
        self.assertEqual(check_shards(self.fetch)["status"], "failed")

    def test_disabled_allocation_fails(self):
        path = "/_cluster/settings?flat_settings=true"
        self.responses[path]["persistent"] = {
            "cluster.routing.allocation.enable": "primaries"
        }
        self.assertEqual(check_shard_allocation(self.fetch)["status"], "failed")

    def test_transient_allocation_overrides_persistent(self):
        path = "/_cluster/settings?flat_settings=true"
        key = "cluster.routing.allocation.enable"
        self.responses[path] = {"persistent": {key: "none"}, "transient": {key: "all"}}
        self.assertEqual(check_shard_allocation(self.fetch)["status"], "passed")

    def test_nodes(self):
        path = "/_cat/nodes?format=json&h=name,version,heap.percent"
        self.assertEqual(check_nodes(self.fetch)["status"], "passed")

        self.responses[path][0]["version"] = "8.19.0"
        self.assertEqual(check_nodes(self.fetch)["status"], "failed")

        self.responses[path][0]["version"] = "8.17.4"
        self.responses[path][0]["heap.percent"] = "80"
        self.assertEqual(check_nodes(self.fetch)["status"], "warning")

        self.responses[path][0]["heap.percent"] = "95"
        self.assertEqual(check_nodes(self.fetch)["status"], "failed")

        self.responses[path] = [
            {"name": "only", "version": "8.17.4", "heap.percent": "10"}
        ]
        self.assertEqual(check_nodes(self.fetch)["status"], "warning")

    def test_disk_thresholds(self):
        path = "/_cat/allocation?format=json&h=node,disk.percent"
        for percent, expected in (
            ("74", "passed"),
            ("75", "warning"),
            ("85", "failed"),
        ):
            with self.subTest(percent=percent):
                self.responses[path][0]["disk.percent"] = percent
                self.assertEqual(check_disk(self.fetch)["status"], expected)

    def test_disk_without_figures_fails(self):
        path = "/_cat/allocation?format=json&h=node,disk.percent"
        self.responses[path] = [{"node": "UNASSIGNED", "disk.percent": None}]
        self.assertEqual(check_disk(self.fetch)["status"], "failed")

    def test_deprecations_include_nested_index_issues(self):
        path = "/_migration/deprecations"
        self.assertEqual(check_deprecations(self.fetch)["status"], "passed")

        self.responses[path]["index_settings"] = {"logs": [{"level": "warning"}]}
        self.assertEqual(check_deprecations(self.fetch)["status"], "warning")

        self.responses[path]["index_settings"] = {"logs": [{"level": "critical"}]}
        self.assertEqual(check_deprecations(self.fetch)["status"], "failed")

    def test_snapshot_age(self):
        path = "/_cat/snapshots?format=json&h=id,status,end_epoch"
        recent = str(NOW - 3600)
        stale = str(NOW - 48 * 3600)
        cases = [
            ([], "warning"),
            ([{"id": "a", "status": "FAILED", "end_epoch": recent}], "warning"),
            ([{"id": "a", "status": "SUCCESS", "end_epoch": stale}], "warning"),
            ([{"id": "a", "status": "SUCCESS", "end_epoch": recent}], "passed"),
        ]
        for snapshots, expected in cases:
            with self.subTest(snapshots=snapshots):
                self.responses[path] = snapshots
                result = check_snapshot(self.fetch, now=NOW)
                self.assertEqual(result["status"], expected)


class TestRunUpgradeChecks(unittest.TestCase):
    def run_with(self, responses, target="8.19.0"):
        def fake_fetch(_url, path, *_args):
            value = responses[path]
            if isinstance(value, Exception):
                raise value
            return value

        with patch("dataguardian.upgrade_checks.fetch_json", fake_fetch):
            return run_upgrade_checks("http://user-host:9200/?a=b", target)

    def statuses(self, report):
        return {check["name"]: check["status"] for check in report["checks"]}

    def test_healthy_cluster_is_ready(self):
        with patch("dataguardian.upgrade_checks.time.time", return_value=9999999999):
            report = self.run_with(healthy_responses())

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["current_version"], "8.17.4")
        self.assertIs(report["changes_made"], False)
        self.assertEqual(report["target"], "http://user-host:9200")

    def test_warning_does_not_block(self):
        responses = healthy_responses()
        responses["/_cat/snapshots?format=json&h=id,status,end_epoch"] = []

        report = self.run_with(responses)

        self.assertEqual(report["status"], "ready_with_warnings")

    def test_one_failed_check_blocks(self):
        responses = healthy_responses()
        responses["/_cluster/health"]["status"] = "red"

        report = self.run_with(responses)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.statuses(report)["cluster_health"], "failed")

    def test_unreachable_endpoint_fails_that_check(self):
        responses = healthy_responses()
        responses["/_migration/deprecations"] = ElasticsearchError("HTTP 403")

        report = self.run_with(responses)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.statuses(report)["deprecations"], "failed")

    def test_malformed_response_fails_instead_of_passing(self):
        responses = healthy_responses()
        responses["/_cluster/health"] = ["unexpected"]
        responses["/_cat/allocation?format=json&h=node,disk.percent"] = {"a": 1}

        report = self.run_with(responses)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.statuses(report)["cluster_health"], "failed")
        self.assertEqual(self.statuses(report)["disk"], "failed")

    def test_missing_version_blocks(self):
        responses = healthy_responses()
        responses["/"] = {"name": "no-version"}

        report = self.run_with(responses)

        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["current_version"])

    def test_cluster_down_blocks_every_check(self):
        down = ElasticsearchError("Could not connect to Elasticsearch")

        report = self.run_with(dict.fromkeys(healthy_responses(), down))

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(set(self.statuses(report).values()), {"failed"})


class TestUpgradeCli(unittest.TestCase):
    def run_cli(self, status):
        report = {"status": status}
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "dataguardian.upgrade_cli.run_upgrade_checks", return_value=report
                ),
                patch("builtins.print") as mocked_print,
            ):
                code = main(
                    [
                        "--url",
                        "http://localhost:9200",
                        "--target-version",
                        "8.19.0",
                        "--report-dir",
                        directory,
                    ]
                )
            printed = json.loads(mocked_print.call_args.args[0])
            self.assertTrue(Path(printed["report_path"]).exists())
        return code

    def test_exit_codes(self):
        cases = [("ready", 0), ("ready_with_warnings", 0), ("blocked", 1)]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(self.run_cli(status), expected)


if __name__ == "__main__":
    unittest.main()
