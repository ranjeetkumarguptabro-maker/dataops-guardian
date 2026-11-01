"""Read-only checks that decide whether Elasticsearch is safe to upgrade."""

import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dataguardian.elasticsearch import ElasticsearchError, fetch_json

DISK_WARNING_PERCENT = 75
# Elasticsearch's default low disk watermark stops new shards being allocated here.
DISK_FAILURE_PERCENT = 85
HEAP_WARNING_PERCENT = 75
HEAP_FAILURE_PERCENT = 90
SNAPSHOT_MAX_AGE_HOURS = 24

VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Fetch = Callable[[str], object]


def _result(name: str, status: str, message: str) -> dict:
    return {"name": name, "status": status, "message": message}


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse a release version such as 8.17.4; pre-releases are not supported."""
    match = VERSION_PATTERN.match(version.strip()) if isinstance(version, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def _minimum_source_version(target: tuple[int, int, int]) -> tuple[int, int] | None:
    """Return the oldest major.minor that may upgrade directly across a major."""
    major, minor, _ = target
    if major == 8:
        return (7, 17)
    if major == 9:
        return (8, 18) if minor == 0 else (8, 19)
    return None


def check_upgrade_path(current_version: str, target_version: str) -> dict:
    """Check the target against Elastic's documented upgrade paths."""
    name = "upgrade_path"
    current = parse_version(current_version)
    target = parse_version(target_version)

    if current is None or target is None:
        return _result(name, "failed", "Versions must look like 8.17.4")
    if target == current:
        return _result(name, "failed", f"Already running {current_version}")
    if target < current:
        return _result(name, "failed", "Elasticsearch does not support downgrades")

    if target[0] == current[0]:
        return _result(
            name, "passed", f"{current_version} to {target_version} is supported"
        )

    minimum = _minimum_source_version(target)
    if minimum is None or current[0] != target[0] - 1:
        return _result(
            name,
            "failed",
            f"No known direct path from {current_version} to {target_version}",
        )
    if current[:2] < minimum:
        return _result(
            name,
            "failed",
            f"Upgrade to {minimum[0]}.{minimum[1]}.x before {target_version}",
        )
    return _result(
        name, "passed", f"{current_version} to {target_version} is supported"
    )


def check_cluster_health(fetch: Fetch) -> dict:
    """Require a green cluster; yellow is tolerated only on a single node."""
    name = "cluster_health"
    health = fetch("/_cluster/health")
    status = health["status"]

    if status == "green":
        return _result(name, "passed", "Cluster health is green")
    if status == "yellow" and health["number_of_nodes"] == 1:
        return _result(
            name, "warning", "Cluster is yellow; a single node cannot host replicas"
        )
    return _result(name, "failed", f"Cluster health is {status}")


def check_shards(fetch: Fetch) -> dict:
    """Block the upgrade while shards are moving or recovering."""
    health = fetch("/_cluster/health")
    moving = health["relocating_shards"] + health["initializing_shards"]

    if moving:
        return _result("shards", "failed", f"{moving} shards are moving or recovering")
    return _result("shards", "passed", "No shards are moving or recovering")


def check_shard_allocation(fetch: Fetch) -> dict:
    """Detect allocation left disabled, for example by an interrupted upgrade."""
    name = "shard_allocation"
    settings = fetch("/_cluster/settings?flat_settings=true")
    key = "cluster.routing.allocation.enable"
    # Transient settings take precedence over persistent ones.
    value = settings["transient"].get(key) or settings["persistent"].get(key) or "all"

    if value != "all":
        return _result(name, "failed", f"Shard allocation is set to '{value}'")
    return _result(name, "passed", "Shard allocation is enabled")


def check_nodes(fetch: Fetch) -> dict:
    """Check that nodes run one version and that heap use is not too high."""
    name = "nodes"
    nodes = fetch("/_cat/nodes?format=json&h=name,version,heap.percent")
    if not nodes:
        return _result(name, "failed", "Elasticsearch reported no nodes")

    versions = sorted({node["version"] for node in nodes})
    if len(versions) > 1:
        return _result(
            name,
            "failed",
            f"Mixed versions found, upgrade may be in progress: {versions}",
        )

    highest = max(int(node["heap.percent"]) for node in nodes)
    if highest >= HEAP_FAILURE_PERCENT:
        return _result(name, "failed", f"Heap use reached {highest}%")
    if highest >= HEAP_WARNING_PERCENT:
        return _result(name, "warning", f"Heap use reached {highest}%")
    if len(nodes) == 1:
        return _result(name, "warning", "Single node: upgrading it will cause downtime")
    return _result(name, "passed", f"{len(nodes)} nodes run version {versions[0]}")


def check_disk(fetch: Fetch) -> dict:
    """Check that every data node has room for shards to move during an upgrade."""
    name = "disk"
    rows = fetch("/_cat/allocation?format=json&h=node,disk.percent")
    # Unassigned shards appear as a row without a disk figure.
    used = [int(row["disk.percent"]) for row in rows if row.get("disk.percent")]
    if not used:
        return _result(name, "failed", "No disk usage was reported")

    highest = max(used)
    if highest >= DISK_FAILURE_PERCENT:
        return _result(name, "failed", f"Disk use reached {highest}%")
    if highest >= DISK_WARNING_PERCENT:
        return _result(name, "warning", f"Disk use reached {highest}%")
    return _result(name, "passed", f"Highest disk use is {highest}%")


def _count_deprecations(section) -> tuple[int, int]:
    """Count critical and warning entries in a deprecation API section."""
    if isinstance(section, dict):
        counts = [_count_deprecations(value) for value in section.values()]
        return sum(c[0] for c in counts), sum(c[1] for c in counts)

    levels = [item.get("level") for item in section]
    return levels.count("critical"), levels.count("warning")


def check_deprecations(fetch: Fetch) -> dict:
    """Block on critical deprecations, which can prevent an upgrade."""
    name = "deprecations"
    critical, warnings = _count_deprecations(fetch("/_migration/deprecations"))

    if critical:
        return _result(name, "failed", f"{critical} critical deprecation issues")
    if warnings:
        return _result(name, "warning", f"{warnings} deprecation warnings")
    return _result(name, "passed", "No deprecation issues")


def check_snapshot(fetch: Fetch, now: float | None = None) -> dict:
    """Warn without a recent backup, because nodes cannot be downgraded."""
    name = "snapshot"
    current = time.time() if now is None else now
    snapshots = fetch("/_cat/snapshots?format=json&h=id,status,end_epoch")
    finished = [
        int(snapshot["end_epoch"])
        for snapshot in snapshots
        if snapshot["status"] == "SUCCESS"
    ]

    if not finished:
        return _result(name, "warning", "No successful snapshot was found")

    age_hours = (current - max(finished)) / 3600
    if age_hours > SNAPSHOT_MAX_AGE_HOURS:
        return _result(
            name,
            "warning",
            f"Latest snapshot is {age_hours:.0f} hours old; "
            f"take one within {SNAPSHOT_MAX_AGE_HOURS} hours of upgrading",
        )
    return _result(name, "passed", f"Latest snapshot is {age_hours:.1f} hours old")


def _run_check(name: str, check: Callable[[], dict]) -> dict:
    """Treat a check that cannot reach a conclusion as failed, never as passed."""
    try:
        return check()
    except ElasticsearchError as exc:
        return _result(name, "failed", str(exc))
    except (AttributeError, KeyError, TypeError, ValueError):
        return _result(name, "failed", "Elasticsearch returned an unexpected response")


def run_upgrade_checks(
    base_url: str,
    target_version: str,
    timeout: float = 5.0,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """Run every readiness check and return a report; nothing is changed."""

    def fetch(path: str):
        return fetch_json(base_url, path, timeout, username, password)

    def current_version() -> str:
        return fetch("/")["version"]["number"]

    current = None
    try:
        current = current_version()
        path_check = check_upgrade_path(current, target_version)
    except ElasticsearchError as exc:
        path_check = _result("upgrade_path", "failed", str(exc))
    except (KeyError, TypeError):
        path_check = _result(
            "upgrade_path", "failed", "Elasticsearch did not report a version"
        )

    checks = [path_check]
    for name, check in (
        ("cluster_health", check_cluster_health),
        ("shards", check_shards),
        ("shard_allocation", check_shard_allocation),
        ("nodes", check_nodes),
        ("disk", check_disk),
        ("deprecations", check_deprecations),
        ("snapshot", check_snapshot),
    ):
        checks.append(_run_check(name, lambda check=check: check(fetch)))

    statuses = {check["status"] for check in checks}
    if "failed" in statuses:
        status = "blocked"
    elif "warning" in statuses:
        status = "ready_with_warnings"
    else:
        status = "ready"

    try:
        parsed = urlparse(base_url.strip())
        target = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            target += f":{parsed.port}"
    except (AttributeError, ValueError):
        target = "<invalid>"

    return {
        "component": "dataguardian-upgrade-checks",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "current_version": current,
        "target_version": target_version,
        "status": status,
        "checks": checks,
        "changes_made": False,
    }


def save_report(report: dict, report_dir: str = "reports") -> str:
    """Write a report to a timestamped JSON file and return its path."""
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = directory / f"upgrade-check-{timestamp}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return str(report_path)
