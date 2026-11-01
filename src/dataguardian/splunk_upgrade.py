"""Upgrade a single Splunk Enterprise instance running on Kubernetes.

Splunk has no downgrade path, so rolling back is only safe when the new version
never started. Any later failure stops and points to the configuration backup.
"""

import argparse
import json
import logging
import os
import tarfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from dataguardian.es_upgrade import (
    IMAGE_FAILURES,
    KubernetesCluster,
    RollingUpgrade,
    UpgradeError,
)
from dataguardian.splunk import publish_report
from dataguardian.upgrade_checks import parse_version, save_report

logger = logging.getLogger("dataguardian.splunk_upgrade")

DISK_WARNING_PERCENT = 80
DISK_FAILURE_PERCENT = 90
REQUIRED_MOUNTS = {"/opt/splunk/etc", "/opt/splunk/var"}
# From Splunk's upgrade guide. Add a line only after checking its table for that
# release; anything not listed here is refused.
DIRECT_UPGRADES = {(10, 0): {(9, 4)}}

API_COMMAND = 'curl -sk -m 30 -u "admin:$SPLUNK_PASSWORD" "https://localhost:8089{}"'
SEARCH_COMMAND = (
    'curl -sk -m 60 -u "admin:$SPLUNK_PASSWORD" '
    "https://localhost:8089/services/search/jobs/export "
    '--data-urlencode "search=search index=main | stats count" '
    "-d output_mode=json"
)
HEC_COMMAND = "curl -fs -m 10 http://localhost:8088/services/collector/health"


class ImageStartFailure(UpgradeError):
    """The new image never ran, so Splunk's data is untouched."""


def check_splunk_path(current_version: str, target_version: str) -> dict:
    """Check the target against Splunk's documented upgrade paths."""
    name = "upgrade_path"
    current = parse_version(current_version)
    target = parse_version(target_version)

    if current is None or target is None:
        status, message = "failed", "Versions must look like 9.4.17"
    elif target == current:
        status, message = "failed", f"Already running {current_version}"
    elif target < current:
        status, message = "failed", "Splunk does not support downgrades"
    elif target[:2] == current[:2] or current[:2] in DIRECT_UPGRADES.get(
        target[:2], set()
    ):
        status = "passed"
        message = f"{current_version} to {target_version} is supported"
    else:
        status = "failed"
        message = f"No known direct path from {current_version} to {target_version}"
    return {"name": name, "status": status, "message": message}


def validate_splunk_statefulset(statefulset: dict) -> list[str]:
    """Return configuration problems that would make an upgrade unsafe."""
    problems = []
    spec = statefulset["spec"]
    if spec["updateStrategy"]["type"] != "OnDelete":
        problems.append("updateStrategy must be OnDelete")
    container = spec["template"]["spec"]["containers"][0]
    if not container.get("readinessProbe"):
        problems.append("No readinessProbe: a restarted Splunk cannot be detected")

    claims = {
        claim["metadata"]["name"] for claim in spec.get("volumeClaimTemplates", [])
    }
    persisted = {
        mount["mountPath"]
        for mount in container.get("volumeMounts", [])
        if mount["name"] in claims
    }
    for path in sorted(REQUIRED_MOUNTS - persisted):
        problems.append(f"{path} is not on a persistent volume")
    return problems


class SplunkUpgrade:
    """One upgrade attempt. Dependencies are injected so failures can be tested."""

    def __init__(
        self,
        cluster: KubernetesCluster,
        target_version: str,
        backup_dir: str,
        step_timeout: float = 900.0,
        poll_interval: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cluster = cluster
        self.target_version = target_version
        self.backup_dir = backup_dir
        self.step_timeout = step_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self.pod = f"{cluster.name}-0"

    def _shell(self, command: str) -> str:
        return self.cluster.exec_in_pod(self.pod, "sh", "-c", command)

    def _api(self, path: str) -> dict:
        output = self._shell(API_COMMAND.format(path))
        try:
            return json.loads(output)["entry"][0]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise UpgradeError(
                f"Splunk returned an invalid response to {path}"
            ) from exc

    def _version(self) -> str:
        return self._api("/services/server/info?output_mode=json")["version"]

    def _health(self) -> str:
        return self._api("/services/server/health/splunkd?output_mode=json")["health"]

    def _event_count(self) -> int:
        results = [
            json.loads(line)["result"]
            for line in self._shell(SEARCH_COMMAND).splitlines()
            if '"result"' in line
        ]
        if not results:
            raise UpgradeError("Splunk search returned no result")
        return int(results[-1]["count"])

    def _disk_percent(self) -> int:
        lines = self._shell("df -P /opt/splunk/var").strip().splitlines()
        return int(lines[-1].split()[4].rstrip("%"))

    def _checks(self, current: str) -> list[dict]:
        checks = [check_splunk_path(current, self.target_version)]

        health = self._health()
        status = {"green": "passed", "yellow": "warning"}.get(health, "failed")
        checks.append(
            {
                "name": "health",
                "status": status,
                "message": f"Splunk health is {health}",
            }
        )

        disk = self._disk_percent()
        if disk >= DISK_FAILURE_PERCENT:
            status = "failed"
        elif disk >= DISK_WARNING_PERCENT:
            status = "warning"
        else:
            status = "passed"
        checks.append(
            {"name": "disk", "status": status, "message": f"Disk use is {disk}%"}
        )
        return checks

    def _back_up(self) -> str:
        """Archive Splunk's configuration, which contains secrets, privately."""
        directory = Path(self.backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"splunk-etc-{timestamp}.tar.gz"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            self.cluster.exec_to_file(
                self.pod,
                output,
                "sudo",
                "-n",
                "tar",
                "czf",
                "-",
                "-C",
                "/opt/splunk",
                "etc",
            )
        try:
            with tarfile.open(path) as archive:
                names = archive.getnames()
        except (tarfile.TarError, OSError) as exc:
            raise UpgradeError("The configuration backup is not readable") from exc
        if "etc/system/local/server.conf" not in names:
            raise UpgradeError("The configuration backup is missing server.conf")
        return str(path)

    def _replace_pod(self, image: str) -> None:
        self.cluster.delete_pod(self.pod)
        deadline = self.clock() + self.step_timeout
        while True:
            current = self.cluster.pod(self.pod)
            if (
                current
                and not current["metadata"].get("deletionTimestamp")
                and current["spec"]["containers"][0]["image"] == image
            ):
                for status in current.get("status", {}).get("containerStatuses", []):
                    reason = status.get("state", {}).get("waiting", {}).get("reason")
                    if reason in IMAGE_FAILURES:
                        raise ImageStartFailure(f"Could not start {image}: {reason}")
                if RollingUpgrade._is_ready(current):
                    return
            if self.clock() >= deadline:
                raise UpgradeError(f"Timed out waiting for Splunk to start on {image}")
            self.sleep(self.poll_interval)

    def _verify(self, events_before: int) -> list[str]:
        problems = []
        version = self._version()
        if version != self.target_version:
            problems.append(f"Splunk reports version {version}")
        health = self._health()
        if health == "red":
            problems.append("Splunk health is red")
        self._shell(HEC_COMMAND)
        events_after = self._event_count()
        if events_after < events_before:
            problems.append(f"Events fell from {events_before} to {events_after}")
        return problems

    def run(self, execute: bool = False, accept_warnings: bool = False) -> dict:
        """Check Splunk, then upgrade it only when execution was requested."""
        report = {
            "component": "dataguardian-splunk-upgrade",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "namespace": self.cluster.namespace,
            "statefulset": self.cluster.name,
            "target_version": self.target_version,
            "status": "failed",
            "message": "",
            "checks": [],
            "changes_made": False,
        }
        try:
            self._run(report, execute, accept_warnings)
        except UpgradeError as exc:
            report["status"] = "failed"
            report["message"] = str(exc)
            if report["changes_made"]:
                report["message"] += (
                    ". Splunk has no downgrade path: restore the configuration "
                    "backup and data onto the old version if it cannot be fixed."
                )
        return report

    def _run(self, report: dict, execute: bool, accept_warnings: bool) -> None:
        statefulset = self.cluster.statefulset()
        problems = validate_splunk_statefulset(statefulset)
        if problems:
            report["status"] = "blocked"
            report["message"] = "Unsafe configuration: " + "; ".join(problems)
            return

        container = statefulset["spec"]["template"]["spec"]["containers"][0]
        previous_image = container["image"]
        image = f"{previous_image.rpartition(':')[0]}:{self.target_version}"

        current = self._version()
        report["current_version"] = current
        report["checks"] = self._checks(current)
        failed = [c["name"] for c in report["checks"] if c["status"] == "failed"]
        warned = [c["name"] for c in report["checks"] if c["status"] == "warning"]

        if failed:
            report["status"] = "blocked"
            report["message"] = f"Failed checks: {', '.join(failed)}"
            return
        if not execute:
            report["status"] = "dry_run"
            report["message"] = "Dry run only; no changes were made"
            return
        if warned and not accept_warnings:
            report["status"] = "blocked"
            report["message"] = f"Warnings need --accept-warnings: {', '.join(warned)}"
            return

        report["config_backup"] = self._back_up()
        events_before = self._event_count()
        report["events_before"] = events_before

        report["changes_made"] = True
        self.cluster.set_image(container["name"], image)
        try:
            self._replace_pod(image)
        except ImageStartFailure as exc:
            logger.warning("Rolling back: %s", exc)
            self.cluster.set_image(container["name"], previous_image)
            self._replace_pod(previous_image)
            if self._version() != current:
                raise UpgradeError(f"Rollback did not restore {current}") from exc
            report["status"] = "rolled_back"
            report["message"] = f"{exc}. Restored {current}; data was not touched."
            return

        deadline = self.clock() + self.step_timeout
        while True:
            try:
                problems = self._verify(events_before)
            except UpgradeError as exc:
                problems = [str(exc)]
            if not problems:
                break
            if self.clock() >= deadline:
                raise UpgradeError("; ".join(problems))
            self.sleep(self.poll_interval)

        report["events_after"] = self._event_count()
        report["status"] = "completed"
        report["message"] = f"Splunk runs {self.target_version}"


def main(argv: list[str] | None = None) -> int:
    """Run a dry run by default; change Splunk only with --execute."""
    parser = argparse.ArgumentParser(
        description="Plan or run a Splunk Enterprise upgrade on Kubernetes."
    )
    parser.add_argument("--namespace", default="splunk")
    parser.add_argument("--statefulset", default="splunk")
    parser.add_argument("--target-version", required=True, help="For example 10.0.12")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--accept-warnings", action="store_true")
    parser.add_argument("--step-timeout", type=float, default=900.0)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument(
        "--backup-dir",
        default="backups",
        help="Private directory for configuration backups, which contain secrets",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    upgrade = SplunkUpgrade(
        KubernetesCluster(args.namespace, args.statefulset),
        args.target_version,
        args.backup_dir,
        step_timeout=args.step_timeout,
    )
    report = upgrade.run(execute=args.execute, accept_warnings=args.accept_warnings)
    report["splunk"] = publish_report(report)
    report["report_path"] = save_report(report, args.report_dir, "splunk-upgrade")

    print(json.dumps(report, indent=2))
    return 0 if report["status"] in {"completed", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
