"""Rolling Elasticsearch upgrades for a StatefulSet running on Kubernetes.

Follows Elastic's rolling upgrade procedure, one node at a time with the elected
master last. Elasticsearch nodes cannot be downgraded, so a failed upgrade stops
and can be resumed; it never tries to roll back.
"""

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dataguardian.elasticsearch import ElasticsearchError, request_json
from dataguardian.upgrade_checks import parse_version, run_upgrade_checks

logger = logging.getLogger("dataguardian.es_upgrade")

ALLOCATION_KEY = "cluster.routing.allocation.enable"
REQUEST_TIMEOUT = 10.0
IMAGE_FAILURES = {"ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull"}
# Checks an interrupted upgrade is expected to fail, because it left the cluster
# with mixed versions, a raised version and possibly restricted allocation.
RESUME_IGNORED_CHECKS = {"upgrade_path", "nodes", "shard_allocation", "cluster_health"}
SETTLED_POLLS = 3
SNAPSHOT_REPOSITORY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class UpgradeError(Exception):
    """The upgrade cannot continue safely."""


def validate_statefulset(statefulset: dict) -> list[str]:
    """Return configuration problems that make a rolling upgrade unsafe."""
    problems = []
    spec = statefulset["spec"]
    replicas = spec["replicas"]
    if replicas < 3 or replicas % 2 == 0:
        problems.append(
            f"{replicas} replicas: an odd number of at least 3 keeps a master "
            "quorum while one node restarts"
        )
    if not spec.get("volumeClaimTemplates"):
        problems.append("No volumeClaimTemplates: data would not survive a restart")

    container = spec["template"]["spec"]["containers"][0]
    if not container.get("readinessProbe"):
        problems.append("No readinessProbe: a restarted node cannot be detected")
    if not container.get("resources", {}).get("limits", {}).get("memory"):
        problems.append("No memory limit is set")

    env = {item["name"]: item.get("value", "") for item in container.get("env", [])}
    heap = dict(re.findall(r"-Xm([sx])(\S+)", env.get("ES_JAVA_OPTS", "")))
    if heap and heap.get("s") != heap.get("x"):
        problems.append("Heap settings -Xms and -Xmx must match")
    return problems


class KubernetesCluster:
    """The kubectl operations needed to upgrade one StatefulSet."""

    def __init__(self, namespace: str, statefulset: str):
        self.namespace = namespace
        self.name = statefulset

    def _kubectl(self, *args: str, timeout: float = 30.0) -> str:
        command = ["kubectl", "-n", self.namespace, *args]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpgradeError(f"kubectl {args[0]} did not complete") from exc
        if result.returncode != 0:
            raise UpgradeError(f"kubectl {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def statefulset(self) -> dict:
        output = self._kubectl("get", "statefulset", self.name, "-o", "json")
        return json.loads(output)

    def set_image(self, container: str, image: str) -> None:
        self._kubectl(
            "set", "image", f"statefulset/{self.name}", f"{container}={image}"
        )

    def delete_pod(self, pod: str) -> None:
        self._kubectl("delete", "pod", pod, "--wait=false")

    def pod(self, pod: str) -> dict | None:
        """Return a pod, or None while it does not exist."""
        output = self._kubectl("get", "pod", pod, "--ignore-not-found", "-o", "json")
        return json.loads(output) if output.strip() else None

    @contextmanager
    def port_forward(self, pod: str) -> Iterator[str]:
        """Yield a local URL for one pod.

        A Service would be cut off when its chosen pod restarts, so each step talks
        to a named pod that is not being restarted.
        """
        process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                self.namespace,
                "port-forward",
                f"pod/{pod}",
                ":9200",
                "--pod-running-timeout=30s",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            line = process.stdout.readline()
            if "127.0.0.1:" not in line:
                raise UpgradeError(f"Could not port-forward to {pod}")
            port = line.split("127.0.0.1:")[1].split()[0]
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()


class RollingUpgrade:
    """One upgrade attempt. Dependencies are injected so failures can be tested."""

    def __init__(
        self,
        cluster: KubernetesCluster,
        target_version: str,
        step_timeout: float = 300.0,
        poll_interval: float = 5.0,
        request: Callable = request_json,
        checks: Callable = run_upgrade_checks,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        snapshot_repository: str | None = None,
        backup_dir: str | None = None,
    ):
        self.cluster = cluster
        self.target_version = target_version
        self.step_timeout = step_timeout
        self.poll_interval = poll_interval
        self.request = request
        self.checks = checks
        self.sleep = sleep
        self.clock = clock
        self.snapshot_repository = snapshot_repository
        self.backup_dir = backup_dir

    def _call(self, url: str, path: str, method: str = "GET", body=None):
        return self.request(
            url, path, REQUEST_TIMEOUT, None, None, method=method, body=body
        )

    def _wait_until(self, condition: Callable[[], bool], description: str) -> None:
        """Poll a condition; requests fail normally while a node restarts."""
        deadline = self.clock() + self.step_timeout
        while True:
            try:
                if condition():
                    return
            except ElasticsearchError as exc:
                logger.debug("Still waiting for %s: %s", description, exc)
            if self.clock() >= deadline:
                raise UpgradeError(f"Timed out waiting for {description}")
            self.sleep(self.poll_interval)

    def _take_snapshot(self, url: str) -> str:
        """Take a snapshot and wait for it; it is the only way back after upgrading."""
        repository = self.snapshot_repository
        if not SNAPSHOT_REPOSITORY_PATTERN.match(repository):
            raise UpgradeError(f"Invalid snapshot repository name: {repository}")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
        name = f"guardian-pre-{self.target_version}-{timestamp}"
        path = f"/_snapshot/{repository}/{name}"
        self._call(url, path, method="PUT", body={"include_global_state": True})

        def finished() -> bool:
            state = self._call(url, path)["snapshots"][0]["state"]
            if state in {"FAILED", "PARTIAL", "INCOMPATIBLE"}:
                raise UpgradeError(f"Snapshot {name} ended as {state}")
            return state == "SUCCESS"

        self._wait_until(finished, f"snapshot {name} to finish")
        logger.info("Snapshot %s is complete", name)
        return name

    def _back_up_config(self, url: str, statefulset: dict) -> str:
        """Save the StatefulSet and cluster settings before anything changes."""
        directory = Path(self.backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"es-config-backup-{timestamp}.json"
        backup = {
            "statefulset": statefulset,
            "cluster_settings": self._call(
                url, "/_cluster/settings?flat_settings=true"
            ),
        }
        path.write_text(json.dumps(backup, indent=2) + "\n", encoding="utf-8")
        return str(path)

    def _set_allocation(self, url: str, value: str | None) -> None:
        def apply() -> bool:
            self._call(
                url,
                "/_cluster/settings",
                method="PUT",
                body={"persistent": {ALLOCATION_KEY: value}},
            )
            return True

        self._wait_until(apply, f"shard allocation to be set to {value or 'default'}")

    def _nodes(self, url: str) -> list[dict]:
        return self._call(url, "/_cat/nodes?format=json&h=name,version")

    def _replace_pod(self, pod: str, image: str) -> None:
        self.cluster.delete_pod(pod)

        def ready() -> bool:
            current = self.cluster.pod(pod)
            if current is None or current["metadata"].get("deletionTimestamp"):
                return False
            if current["spec"]["containers"][0]["image"] != image:
                return False
            for status in current.get("status", {}).get("containerStatuses", []):
                reason = status.get("state", {}).get("waiting", {}).get("reason")
                if reason in IMAGE_FAILURES:
                    raise UpgradeError(f"{pod} could not start {image}: {reason}")
            return self._is_ready(current)

        self._wait_until(ready, f"{pod} to become ready on {image}")

    def _wait_settled(self, url: str) -> None:
        """Wait until shards have finished recovering.

        Replicas on a newer node cannot be copied to an older one, so a yellow
        cluster is the expected end state while versions are mixed.
        """
        stable = {"polls": 0}

        def settled() -> bool:
            health = self._call(url, "/_cluster/health")
            if health["status"] == "green":
                return True
            mixed = len({node["version"] for node in self._nodes(url)}) > 1
            moving = (
                health["relocating_shards"]
                + health["initializing_shards"]
                + health.get("delayed_unassigned_shards", 0)
            )
            if health["status"] == "yellow" and mixed and not moving:
                stable["polls"] += 1
            else:
                stable["polls"] = 0
            return stable["polls"] >= SETTLED_POLLS

        try:
            self._wait_until(settled, "shards to finish recovering")
        except UpgradeError as exc:
            raise UpgradeError(f"{exc}. {self._allocation_reason(url)}") from exc

    def _allocation_reason(self, url: str) -> str:
        """Ask Elasticsearch why a shard is unassigned, for the failure report."""
        try:
            explain = self._call(url, "/_cluster/allocation/explain")
            return explain.get("allocate_explanation", "No explanation was given")
        except ElasticsearchError:
            return "Elasticsearch could not explain the allocation"

    def _upgrade_node(self, pod: str, others: list[str], image: str) -> None:
        with self.cluster.port_forward(self._ready_pod(others)) as url:
            logger.info("Upgrading %s", pod)
            self._set_allocation(url, "primaries")
            try:
                self._call(url, "/_flush", method="POST")
            except ElasticsearchError as exc:
                # A flush only shortens recovery, so it should not stop the upgrade.
                logger.warning("Flush before restarting %s failed: %s", pod, exc)

            self._replace_pod(pod, image)

            def rejoined() -> bool:
                return any(
                    node["name"] == pod and node["version"] == self.target_version
                    for node in self._nodes(url)
                )

            self._wait_until(rejoined, f"{pod} to rejoin on {self.target_version}")
            self._set_allocation(url, None)
            self._wait_settled(url)
            logger.info("%s now runs %s", pod, self.target_version)

    def _verify(self, url: str, documents_before: int) -> list[str]:
        """Return every way the finished cluster differs from what is expected."""

        def green() -> bool:
            health = self._call(url, "/_cluster/health")
            return health["status"] == "green"

        problems = []
        try:
            self._wait_until(green, "the cluster to turn green")
        except UpgradeError as exc:
            problems.append(str(exc))

        versions = {node["version"] for node in self._nodes(url)}
        if versions != {self.target_version}:
            problems.append(f"Nodes report versions {sorted(versions)}")

        settings = self._call(url, "/_cluster/settings?flat_settings=true")
        if settings["persistent"].get(ALLOCATION_KEY) not in (None, "all"):
            problems.append("Shard allocation was not restored")

        documents_after = self._call(url, "/_count")["count"]
        if documents_after < documents_before:
            problems.append(
                f"Document count fell from {documents_before} to {documents_after}"
            )
        return problems

    def run(
        self,
        execute: bool = False,
        accept_warnings: bool = False,
        resume: bool = False,
    ) -> dict:
        """Check the cluster, then upgrade it only when execution was requested."""
        report = {
            "component": "dataguardian-es-upgrade",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "namespace": self.cluster.namespace,
            "statefulset": self.cluster.name,
            "target_version": self.target_version,
            "resume": resume,
            "status": "failed",
            "message": "",
            "checks": [],
            "plan": [],
            "upgraded_nodes": [],
            "changes_made": False,
        }
        try:
            self._run(report, execute, accept_warnings, resume)
        except (UpgradeError, ElasticsearchError) as exc:
            report["status"] = "failed"
            report["message"] = str(exc)
            if report["changes_made"]:
                report["message"] += (
                    ". Nodes cannot be downgraded: fix the cause and run again "
                    "with --resume."
                )
        return report

    def _run(
        self, report: dict, execute: bool, accept_warnings: bool, resume: bool
    ) -> None:
        statefulset = self.cluster.statefulset()
        strategy = statefulset["spec"]["updateStrategy"]["type"]
        if strategy != "OnDelete":
            raise UpgradeError(
                f"updateStrategy must be OnDelete, not {strategy}: Kubernetes would "
                "restart nodes without preparing the cluster"
            )

        container = statefulset["spec"]["template"]["spec"]["containers"][0]
        repository = container["image"].rpartition(":")[0]
        image = f"{repository}:{self.target_version}"
        pods = [
            f"{self.cluster.name}-{index}"
            for index in range(statefulset["spec"]["replicas"])
        ]
        if len(pods) < 2:
            raise UpgradeError("A rolling upgrade needs at least two nodes")

        config_problems = validate_statefulset(statefulset)
        report["config_problems"] = config_problems
        if config_problems:
            report["status"] = "blocked"
            report["message"] = "Unsafe configuration: " + "; ".join(config_problems)
            return

        with self.cluster.port_forward(self._ready_pod(pods)) as url:
            gate = self.checks(url, self.target_version)
            report["checks"] = gate["checks"]
            master = self._call(url, "/_cat/master?format=json&h=node")[0]["node"]
            documents_before = self._call(url, "/_count")["count"]
            if resume:
                self._require_resumable(url)

        failed = [
            check["name"]
            for check in gate["checks"]
            if check["status"] == "failed"
            and not (resume and check["name"] in RESUME_IGNORED_CHECKS)
        ]
        # Taking a snapshot below resolves the missing snapshot warning.
        warned = [
            c["name"]
            for c in gate["checks"]
            if c["status"] == "warning"
            and not (self.snapshot_repository and c["name"] == "snapshot")
        ]
        if failed:
            report["status"] = "blocked"
            report["message"] = f"Failed checks: {', '.join(failed)}"
            return

        # The elected master moves last, so the cluster holds one election only.
        order = [pod for pod in pods if pod != master] + [
            pod for pod in pods if pod == master
        ]
        remaining = []
        for pod in order:
            # A pod on the target image that never became ready still needs work.
            current = self.cluster.pod(pod)
            if (
                current
                and current["spec"]["containers"][0]["image"] == image
                and self._is_ready(current)
            ):
                continue
            remaining.append(pod)
        report["plan"] = remaining

        if not execute:
            report["status"] = "dry_run"
            report["message"] = "Dry run only; no changes were made"
            return
        if warned and not accept_warnings:
            report["status"] = "blocked"
            report["message"] = f"Warnings need --accept-warnings: {', '.join(warned)}"
            return

        with self.cluster.port_forward(self._ready_pod(pods)) as url:
            if self.backup_dir:
                report["config_backup"] = self._back_up_config(url, statefulset)
            if self.snapshot_repository:
                report["snapshot"] = self._take_snapshot(url)

        report["changes_made"] = True
        self.cluster.set_image(container["name"], image)

        current_pod = None
        try:
            for pod in remaining:
                current_pod = pod
                others = [other for other in order if other != pod]
                self._upgrade_node(pod, others, image)
                report["upgraded_nodes"].append(pod)
        except (UpgradeError, ElasticsearchError):
            self._restore_allocation(current_pod, pods)
            raise

        with self.cluster.port_forward(order[0]) as url:
            problems = self._verify(url, documents_before)
        if problems:
            raise UpgradeError("; ".join(problems))

        report["status"] = "completed"
        report["message"] = f"All nodes run {self.target_version}"

    def _require_resumable(self, url: str) -> None:
        health = self._call(url, "/_cluster/health")
        if health["status"] == "red":
            raise UpgradeError("Cannot resume while the cluster is red")

        # A failed target can be replaced, but nodes can never move backwards.
        target = parse_version(self.target_version)
        versions = [parse_version(node["version"]) for node in self._nodes(url)]
        if target is None or any(version > target for version in versions):
            raise UpgradeError(
                f"Some nodes already run a version newer than {self.target_version}"
            )

    @staticmethod
    def _is_ready(pod: dict) -> bool:
        return any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )

    def _ready_pod(self, candidates: list[str]) -> str:
        """Return a pod able to answer requests."""
        for name in candidates:
            pod = self.cluster.pod(name)
            if pod and self._is_ready(pod):
                return name
        raise UpgradeError(f"None of these pods is ready: {', '.join(candidates)}")

    def _restore_allocation(self, failed_pod: str | None, pods: list[str]) -> None:
        """Let replicas recover while the failed node is being investigated."""
        try:
            survivor = self._ready_pod([pod for pod in pods if pod != failed_pod])
            with self.cluster.port_forward(survivor) as url:
                self._call(
                    url,
                    "/_cluster/settings",
                    method="PUT",
                    body={"persistent": {ALLOCATION_KEY: None}},
                )
        except (UpgradeError, ElasticsearchError) as exc:
            logger.error("Could not restore shard allocation: %s", exc)
