"""Report CPU, memory, disk and pod health for the platform."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from os import getenv

from dataguardian.elasticsearch import ElasticsearchError, fetch_json, request_json
from dataguardian.splunk import check_splunk_health, publish_report
from dataguardian.upgrade_checks import save_report

CPU_WARNING_PERCENT = 90
HEAP_WARNING_PERCENT = 75
DISK_WARNING_PERCENT = 75
POD_RESTART_WARNING = 3
REPORT_INDEX = "guardian-reports"


def collect_elasticsearch(url: str, timeout: float = 5.0) -> tuple[dict, list[str]]:
    """Return per-node resource use and any alerts it raises."""
    health = fetch_json(url, "/_cluster/health", timeout)
    nodes = fetch_json(
        url,
        "/_cat/nodes?format=json&h=name,version,cpu,ram.percent,heap.percent",
        timeout,
    )
    disks = fetch_json(url, "/_cat/allocation?format=json&h=node,disk.percent", timeout)
    disk_by_node = {
        row["node"]: int(row["disk.percent"])
        for row in disks
        if row.get("disk.percent")
    }

    alerts = []
    if health["status"] != "green":
        alerts.append(f"Cluster health is {health['status']}")

    summary = []
    for node in nodes:
        name = node["name"]
        cpu, heap = int(node["cpu"]), int(node["heap.percent"])
        disk = disk_by_node.get(name)
        summary.append(
            {
                "name": name,
                "version": node["version"],
                "cpu_percent": cpu,
                "ram_percent": int(node["ram.percent"]),
                "heap_percent": heap,
                "disk_percent": disk,
            }
        )
        if cpu >= CPU_WARNING_PERCENT:
            alerts.append(f"{name} CPU is at {cpu}%")
        if heap >= HEAP_WARNING_PERCENT:
            alerts.append(f"{name} heap is at {heap}%")
        if disk is not None and disk >= DISK_WARNING_PERCENT:
            alerts.append(f"{name} disk is at {disk}%")

    return {"cluster_health": health["status"], "nodes": summary}, alerts


def summarize_pods(pod_list: dict) -> tuple[list[dict], list[str]]:
    """Summarize `kubectl get pods -o json` and raise alerts for unhealthy pods."""
    pods = []
    alerts = []
    for item in pod_list["items"]:
        name = item["metadata"]["name"]
        status = item.get("status", {})
        restarts = sum(
            container.get("restartCount", 0)
            for container in status.get("containerStatuses", [])
        )
        ready = any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in status.get("conditions", [])
        )
        pods.append({"name": name, "ready": ready, "restarts": restarts})
        if not ready:
            alerts.append(f"Pod {name} is not ready")
        if restarts >= POD_RESTART_WARNING:
            alerts.append(f"Pod {name} restarted {restarts} times")
    return pods, alerts


def collect_pods(namespace: str) -> tuple[list[dict], list[str]]:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "pods", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return summarize_pods(json.loads(result.stdout))


def collect_status(
    url: str, namespace: str | None = None, splunk_url: str | None = None
) -> dict:
    """Collect everything configured; an unreachable component becomes an alert."""
    report = {
        "component": "dataguardian-platform-status",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "alerts": [],
    }
    try:
        report["elasticsearch"], alerts = collect_elasticsearch(url)
        report["alerts"].extend(alerts)
    except (ElasticsearchError, KeyError, TypeError, ValueError) as exc:
        report["alerts"].append(f"Elasticsearch status unavailable: {exc}")

    if namespace:
        try:
            report["pods"], alerts = collect_pods(namespace)
            report["alerts"].extend(alerts)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            report["alerts"].append("Kubernetes pod status unavailable")

    if splunk_url:
        splunk = check_splunk_health(splunk_url)
        report["splunk"] = splunk
        if splunk["status"] != "passed":
            report["alerts"].append(f"Splunk: {splunk['message']}")

    report["status"] = "alert" if report["alerts"] else "healthy"
    return report


def index_report(url: str, report: dict, timeout: float = 5.0) -> dict:
    """Store a report in Elasticsearch so Kibana can chart it over time."""
    document = {"@timestamp": report["generated_at"], **report}
    try:
        request_json(
            url,
            f"/{REPORT_INDEX}/_doc",
            timeout,
            method="POST",
            body=document,
        )
    except ElasticsearchError as exc:
        return {"status": "failed", "message": str(exc)}
    return {"status": "passed", "message": f"Stored in {REPORT_INDEX}"}


def main(argv: list[str] | None = None) -> int:
    """Print platform status and exit non-zero when anything raises an alert."""
    parser = argparse.ArgumentParser(
        description="Report CPU, memory, disk and pod health for the platform."
    )
    parser.add_argument("--url", required=True, help="Elasticsearch URL")
    parser.add_argument("--namespace", help="Kubernetes namespace to check")
    parser.add_argument(
        "--store",
        action="store_true",
        help=f"Also store the report in the {REPORT_INDEX} index",
    )
    parser.add_argument("--report-dir", default="reports")
    args = parser.parse_args(argv)

    report = collect_status(args.url, args.namespace, getenv("SPLUNK_HEC_URL"))
    if args.store:
        report["stored"] = index_report(args.url, report)
    report["splunk_delivery"] = publish_report(report)
    report["report_path"] = save_report(report, args.report_dir, "status")

    print(json.dumps(report, indent=2))
    return 1 if report["status"] == "alert" else 0


if __name__ == "__main__":
    raise SystemExit(main())
