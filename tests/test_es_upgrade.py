import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from dataguardian.elasticsearch import ElasticsearchError
from dataguardian.es_upgrade import (
    ALLOCATION_KEY,
    RollingUpgrade,
    UpgradeError,
    validate_statefulset,
)

REPOSITORY = "docker.elastic.co/elasticsearch/elasticsearch"
OLD = "8.17.4"
NEW = "8.19.12"


class FakeCluster:
    """Simulates a StatefulSet and the Elasticsearch nodes running inside it."""

    namespace = "elasticsearch"
    name = "es"

    def __init__(self, strategy="OnDelete", versions=None, deployed=OLD):
        self.strategy = strategy
        self.versions = versions or {"es-0": OLD, "es-1": OLD, "es-2": OLD}
        self.template_image = f"{REPOSITORY}:{deployed}"
        self.images = {
            pod: f"{REPOSITORY}:{version}" for pod, version in self.versions.items()
        }
        self.master = "es-0"
        self.documents = 100
        self.allocation = None
        self.transient_allocation = None
        self.health = "green"
        self.events = []
        self.fail_image_on = None
        self.fail_rejoin_on = None
        self.pod_waiting = {}
        self.forward_targets = []
        self.snapshot_state = "SUCCESS"

    def statefulset(self):
        return {
            "spec": {
                "replicas": len(self.versions),
                "updateStrategy": {"type": self.strategy},
                "volumeClaimTemplates": [{"metadata": {"name": "data"}}],
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "elasticsearch",
                                "image": self.template_image,
                                "readinessProbe": {"httpGet": {}},
                                "resources": {"limits": {"memory": "768Mi"}},
                                "env": [
                                    {
                                        "name": "ES_JAVA_OPTS",
                                        "value": "-Xms256m -Xmx256m",
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        }

    def set_image(self, container, image):
        self.events.append(f"set_image:{image}")
        self.template_image = image

    def delete_pod(self, pod):
        self.events.append(f"delete:{pod}:allocation={self.allocation}")
        self.images[pod] = self.template_image
        if self.fail_image_on == pod:
            self.pod_waiting[pod] = "ImagePullBackOff"
        elif self.fail_rejoin_on != pod:
            self.versions[pod] = self.template_image.rpartition(":")[2]

    def pod(self, pod):
        waiting = self.pod_waiting.get(pod)
        state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
        ready = "False" if waiting else "True"
        return {
            "metadata": {},
            "spec": {"containers": [{"image": self.images[pod]}]},
            "status": {
                "containerStatuses": [{"state": state}],
                "conditions": [{"type": "Ready", "status": ready}],
            },
        }

    @contextmanager
    def port_forward(self, pod):
        self.forward_targets.append(pod)
        yield f"http://{pod}"

    def request(self, _url, path, *_args, method="GET", body=None):
        if path.startswith("/_snapshot/"):
            if method == "PUT":
                self.events.append(f"snapshot:{self.template_image}")
                return {"accepted": True}
            return {"snapshots": [{"state": self.snapshot_state}]}

        if path == "/_cluster/settings" and method == "PUT":
            persistent = body.get("persistent", {})
            transient = body.get("transient", {})

            if ALLOCATION_KEY in persistent:
                self.allocation = persistent[ALLOCATION_KEY]

            if ALLOCATION_KEY in transient:
                self.transient_allocation = transient[ALLOCATION_KEY]

            self.events.append(
                f"allocation:{self.allocation}:{self.transient_allocation}"
            )
            return {"acknowledged": True}

        if path == "/_flush":
            self.events.append("flush")
            return {}

        if path == "/_cluster/allocation/explain":
            return {"allocate_explanation": "disk watermark exceeded"}

        if path == "/_cat/master?format=json&h=node":
            return [{"node": self.master}]

        if path == "/_count":
            return {"count": self.documents}

        if path == "/_cat/nodes?format=json&h=name,version":
            return [
                {"name": pod, "version": version}
                for pod, version in self.versions.items()
            ]

        if path == "/_cluster/health":
            mixed = len(set(self.versions.values())) > 1
            status = self.health
            if status == "green" and mixed:
                status = "yellow"
            return {"status": status, "relocating_shards": 0, "initializing_shards": 0}

        if path == "/_cluster/settings?flat_settings=true":
            persistent = {ALLOCATION_KEY: self.allocation} if self.allocation else {}
            transient = (
                {ALLOCATION_KEY: self.transient_allocation}
                if self.transient_allocation
                else {}
            )
            return {"persistent": persistent, "transient": transient}

        raise AssertionError(f"Unexpected request: {method} {path}")


def gate(*failed, warnings=()):
    names = ["upgrade_path", "cluster_health", "nodes", "shard_allocation", "disk"]
    checks = [{"name": name, "status": "passed", "message": ""} for name in names]
    for check in checks:
        if check["name"] in failed:
            check["status"] = "failed"
        if check["name"] in warnings:
            check["status"] = "warning"
    return lambda _url, _target: {"checks": checks}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RollingUpgradeTests(unittest.TestCase):
    def upgrade(self, cluster, checks=None, target=NEW, **options):
        clock = Clock()
        return RollingUpgrade(
            cluster,
            target,
            step_timeout=60,
            poll_interval=5,
            request=cluster.request,
            checks=checks or gate(),
            sleep=clock.sleep,
            clock=clock,
            **options,
        )

    def test_dry_run_changes_nothing_and_plans_master_last(self):
        cluster = FakeCluster()

        report = self.upgrade(cluster).run()

        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["plan"], ["es-1", "es-2", "es-0"])
        self.assertIs(report["changes_made"], False)
        self.assertEqual(cluster.events, [])

    def test_execute_upgrades_each_node_in_order(self):
        cluster = FakeCluster()

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "completed", msg=report["message"])
        self.assertEqual(report["upgraded_nodes"], ["es-1", "es-2", "es-0"])
        self.assertEqual(set(cluster.versions.values()), {NEW})
        self.assertIsNone(cluster.allocation)
        self.assertIsNone(cluster.transient_allocation)

        deletions = [e for e in cluster.events if e.startswith("delete")]
        self.assertEqual(
            deletions,
            [f"delete:{pod}:allocation=primaries" for pod in report["upgraded_nodes"]],
        )

    def test_image_changes_before_any_pod_is_deleted(self):
        cluster = FakeCluster()

        self.upgrade(cluster).run(execute=True)

        self.assertTrue(cluster.events[0].startswith("set_image"))

    def test_rolling_update_strategy_is_refused(self):
        cluster = FakeCluster(strategy="RollingUpdate")

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("OnDelete", report["message"])
        self.assertEqual(cluster.events, [])

    def test_failed_check_blocks_before_any_change(self):
        cluster = FakeCluster()

        report = self.upgrade(cluster, gate("disk")).run(execute=True)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("disk", report["message"])
        self.assertEqual(cluster.events, [])

    def test_warning_requires_acceptance(self):
        checks = gate(warnings=("disk",))

        cluster = FakeCluster()
        report = self.upgrade(cluster, checks).run(execute=True)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(cluster.events, [])

        cluster = FakeCluster()
        report = self.upgrade(cluster, checks).run(execute=True, accept_warnings=True)
        self.assertEqual(report["status"], "completed")

    def test_bad_image_stops_and_leaves_other_nodes_untouched(self):
        cluster = FakeCluster()
        cluster.fail_image_on = "es-2"

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("ImagePullBackOff", report["message"])
        self.assertIn("--resume", report["message"])
        self.assertEqual(report["upgraded_nodes"], ["es-1"])
        self.assertEqual(cluster.versions["es-0"], OLD)
        self.assertIsNone(cluster.allocation)
        self.assertIsNone(cluster.transient_allocation)

    def test_node_that_never_rejoins_times_out(self):
        cluster = FakeCluster()
        cluster.fail_rejoin_on = "es-1"

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("rejoin", report["message"])
        self.assertEqual(report["upgraded_nodes"], [])

    def test_never_talks_to_the_pod_being_restarted(self):
        cluster = FakeCluster()
        restarted_through = []
        original = cluster.delete_pod

        def delete(pod):
            restarted_through.append((pod, cluster.forward_targets[-1]))
            original(pod)

        cluster.delete_pod = delete

        self.upgrade(cluster).run(execute=True)

        self.assertEqual(len(restarted_through), 3)
        for pod, target in restarted_through:
            self.assertNotEqual(pod, target)

    def test_lost_documents_fail_verification(self):
        cluster = FakeCluster()
        original = cluster.delete_pod

        def delete(pod):
            cluster.documents = 50
            original(pod)

        cluster.delete_pod = delete

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("Document count fell", report["message"])

    def test_cluster_not_green_after_upgrade_fails(self):
        cluster = FakeCluster()
        original = cluster.delete_pod

        def delete(pod):
            original(pod)
            if set(cluster.versions.values()) == {NEW}:
                cluster.health = "yellow"

        cluster.delete_pod = delete

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")

    def test_stuck_shards_report_the_reason(self):
        cluster = FakeCluster()
        cluster.health = "red"

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("disk watermark exceeded", report["message"])

    def test_flush_failure_does_not_stop_upgrade(self):
        cluster = FakeCluster()
        original = cluster.request

        def request(url, path, *args, **kwargs):
            if path == "/_flush":
                raise ElasticsearchError("HTTP 503")
            return original(url, path, *args, **kwargs)

        cluster.request = request

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "completed", msg=report["message"])

    def test_resume_skips_nodes_already_upgraded(self):
        cluster = FakeCluster(
            versions={"es-0": OLD, "es-1": NEW, "es-2": OLD}, deployed=NEW
        )

        report = self.upgrade(cluster, gate("nodes", "upgrade_path")).run(
            execute=True, resume=True
        )

        self.assertEqual(report["status"], "completed", msg=report["message"])
        self.assertEqual(report["upgraded_nodes"], ["es-2", "es-0"])

    def test_mixed_versions_block_without_resume(self):
        cluster = FakeCluster(
            versions={"es-0": OLD, "es-1": NEW, "es-2": OLD}, deployed=NEW
        )

        report = self.upgrade(cluster, gate("nodes")).run(execute=True)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(cluster.events, [])

    def test_resume_cannot_move_nodes_backwards(self):
        cluster = FakeCluster(
            versions={"es-0": OLD, "es-1": NEW, "es-2": OLD}, deployed=NEW
        )

        report = self.upgrade(cluster, target="8.19.0").run(execute=True, resume=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("newer", report["message"])
        self.assertEqual(cluster.events, [])

    def test_bad_image_can_be_resumed_with_a_good_version(self):
        cluster = FakeCluster()
        cluster.fail_image_on = "es-1"

        failed = self.upgrade(cluster, target="8.19.99").run(execute=True)

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(cluster.versions["es-1"], OLD)

        cluster.fail_image_on = None
        cluster.pod_waiting = {}
        resumed = self.upgrade(cluster, target=NEW).run(execute=True, resume=True)

        self.assertEqual(resumed["status"], "completed", msg=resumed["message"])
        self.assertEqual(set(cluster.versions.values()), {NEW})

    def test_resume_retries_a_pod_on_target_that_never_became_ready(self):
        cluster = FakeCluster(
            versions={"es-0": NEW, "es-1": OLD, "es-2": NEW}, deployed=NEW
        )
        cluster.images["es-1"] = f"{REPOSITORY}:{NEW}"
        cluster.pod_waiting["es-1"] = "CrashLoopBackOff"
        original = cluster.delete_pod

        def delete(pod):
            cluster.pod_waiting.pop(pod, None)
            original(pod)

        cluster.delete_pod = delete

        report = self.upgrade(cluster).run(execute=True, resume=True)

        self.assertEqual(report["status"], "completed", msg=report["message"])
        self.assertEqual(report["upgraded_nodes"], ["es-1"])

    def test_requests_avoid_a_pod_that_is_not_ready(self):
        cluster = FakeCluster()
        cluster.pod_waiting["es-0"] = "CrashLoopBackOff"

        self.upgrade(cluster).run()

        self.assertEqual(cluster.forward_targets, ["es-1"])

    def test_no_ready_pods_fails_without_changes(self):
        cluster = FakeCluster()
        cluster.pod_waiting = dict.fromkeys(cluster.versions, "CrashLoopBackOff")

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(cluster.events, [])

    def test_resume_refuses_red_cluster(self):
        cluster = FakeCluster(deployed=NEW)
        cluster.health = "red"

        report = self.upgrade(cluster).run(execute=True, resume=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("red", report["message"])
        self.assertEqual(cluster.events, [])

    def test_resume_still_respects_other_failed_checks(self):
        cluster = FakeCluster(deployed=NEW)

        report = self.upgrade(cluster, gate("disk")).run(execute=True, resume=True)

        self.assertEqual(report["status"], "blocked")

    def test_snapshot_is_taken_before_the_image_changes(self):
        cluster = FakeCluster()

        report = self.upgrade(cluster, snapshot_repository="guardian").run(execute=True)

        self.assertEqual(report["status"], "completed", msg=report["message"])
        self.assertIn(NEW, report["snapshot"])
        self.assertTrue(cluster.events[0].endswith(OLD))
        self.assertTrue(cluster.events[1].startswith("set_image"))

    def test_failed_snapshot_stops_before_any_change(self):
        cluster = FakeCluster()
        cluster.snapshot_state = "PARTIAL"

        report = self.upgrade(cluster, snapshot_repository="guardian").run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIs(report["changes_made"], False)
        self.assertFalse([e for e in cluster.events if "set_image" in e])

    def test_invalid_snapshot_repository_is_refused(self):
        cluster = FakeCluster()

        report = self.upgrade(cluster, snapshot_repository="../evil").run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(cluster.events, [])

    def test_planned_snapshot_clears_the_snapshot_warning(self):
        cluster = FakeCluster()
        snapshot_warning = {"name": "snapshot", "status": "warning", "message": ""}

        def checks(_url, _target):
            return {"checks": [snapshot_warning]}

        report = self.upgrade(cluster, checks, snapshot_repository="guardian").run(
            execute=True
        )

        self.assertEqual(report["status"], "completed", msg=report["message"])

    def test_config_backup_is_written_before_changes(self):
        cluster = FakeCluster()
        with tempfile.TemporaryDirectory() as directory:
            report = self.upgrade(cluster, backup_dir=directory).run(execute=True)
            backup = json.loads(Path(report["config_backup"]).read_text())

        self.assertEqual(
            backup["statefulset"]["spec"]["template"]["spec"]["containers"][0]["name"],
            "elasticsearch",
        )
        self.assertIn("cluster_settings", backup)

    def test_dry_run_writes_no_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.upgrade(FakeCluster(), backup_dir=directory).run()
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertNotIn("config_backup", report)

    def test_unsafe_config_blocks_before_any_change(self):
        cluster = FakeCluster()
        original = cluster.statefulset

        def without_probe():
            statefulset = original()
            container = statefulset["spec"]["template"]["spec"]["containers"][0]
            del container["readinessProbe"]
            return statefulset

        cluster.statefulset = without_probe

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("readinessProbe", report["message"])
        self.assertEqual(cluster.events, [])

    def test_validate_statefulset_problems(self):
        def problems(mutate):
            statefulset = FakeCluster().statefulset()
            mutate(statefulset["spec"])
            return " ".join(validate_statefulset(statefulset))

        def container(spec):
            return spec["template"]["spec"]["containers"][0]

        self.assertEqual(problems(lambda spec: None), "")
        self.assertIn("quorum", problems(lambda s: s.update(replicas=4)))
        self.assertIn("volumeClaim", problems(lambda s: s.pop("volumeClaimTemplates")))
        self.assertIn("memory", problems(lambda s: container(s).pop("resources")))
        self.assertIn(
            "Xms",
            problems(
                lambda s: container(s)["env"][0].update(value="-Xms1g -Xmx2g"),
            ),
        )

    def test_single_node_is_refused(self):
        cluster = FakeCluster(versions={"es-0": OLD})

        report = self.upgrade(cluster).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("two nodes", report["message"])

    def test_upgrade_error_type_is_reported_not_raised(self):
        cluster = FakeCluster()

        def broken():
            raise UpgradeError("kubectl get failed")

        cluster.statefulset = broken

        report = self.upgrade(cluster).run()

        self.assertEqual(report["status"], "failed")
        self.assertIs(report["changes_made"], False)


if __name__ == "__main__":
    unittest.main()
