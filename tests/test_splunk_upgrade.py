import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

from dataguardian.es_upgrade import UpgradeError
from dataguardian.splunk_upgrade import (
    SplunkUpgrade,
    check_splunk_path,
    validate_splunk_statefulset,
)

REPOSITORY = "splunk/splunk"
OLD = "9.4.17"
NEW = "10.0.12"


def backup_bytes(include_server_conf=True):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        name = "etc/system/local/server.conf" if include_server_conf else "etc/other"
        info = tarfile.TarInfo(name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


class FakeSplunk:
    namespace = "splunk"
    name = "splunk"

    def __init__(self):
        self.version = OLD
        self.image = f"{REPOSITORY}:{OLD}"
        self.pod_image = self.image
        self.health = "green"
        self.disk = 40
        self.events = 50
        self.log = []
        self.broken_tags = set()
        self.waiting = None
        self.backup = backup_bytes()
        self.lose_events = False
        self.never_ready = False

    def statefulset(self):
        return {
            "spec": {
                "updateStrategy": {"type": "OnDelete"},
                "volumeClaimTemplates": [
                    {"metadata": {"name": "etc"}},
                    {"metadata": {"name": "var"}},
                ],
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "splunk",
                                "image": self.image,
                                "readinessProbe": {"httpGet": {}},
                                "volumeMounts": [
                                    {"name": "etc", "mountPath": "/opt/splunk/etc"},
                                    {"name": "var", "mountPath": "/opt/splunk/var"},
                                ],
                            }
                        ]
                    }
                },
            }
        }

    def set_image(self, _container, image):
        self.log.append(f"set_image:{image}")
        self.image = image

    def delete_pod(self, pod):
        self.log.append(f"delete:{pod}")
        self.pod_image = self.image
        tag = self.image.rpartition(":")[2]
        self.waiting = "ImagePullBackOff" if tag in self.broken_tags else None
        if not self.waiting:
            self.version = tag
            if self.lose_events:
                self.events = 1

    def pod(self, _pod):
        state = {"waiting": {"reason": self.waiting}} if self.waiting else {}
        ready = "False" if self.waiting or self.never_ready else "True"
        return {
            "metadata": {},
            "spec": {"containers": [{"image": self.pod_image}]},
            "status": {
                "containerStatuses": [{"state": state}],
                "conditions": [{"type": "Ready", "status": ready}],
            },
        }

    def exec_in_pod(self, _pod, *command):
        text = command[-1]
        if "server/info" in text:
            return json.dumps({"entry": [{"content": {"version": self.version}}]})
        if "server/health" in text:
            return json.dumps({"entry": [{"content": {"health": self.health}}]})
        if "jobs/export" in text:
            return json.dumps({"result": {"count": str(self.events)}})
        if text.startswith("df"):
            return f"Filesystem 1 2 3 Use% On\n/dev/x 1 1 1 {self.disk}% /opt\n"
        if "collector/health" in text:
            return "{}"
        raise AssertionError(text)

    def exec_to_file(self, _pod, output, *_command):
        self.log.append("backup")
        output.write(self.backup)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SplunkPathTests(unittest.TestCase):
    def test_paths(self):
        cases = [
            (OLD, NEW, "passed"),
            ("9.4.1", "9.4.17", "passed"),
            ("9.3.5", NEW, "failed"),
            (OLD, "10.2.9", "failed"),
            (NEW, OLD, "failed"),
            (OLD, OLD, "failed"),
            (OLD, "latest", "failed"),
        ]
        for current, target, expected in cases:
            with self.subTest(current=current, target=target):
                result = check_splunk_path(current, target)
                self.assertEqual(result["status"], expected)


class SplunkValidationTests(unittest.TestCase):
    def test_valid_configuration(self):
        self.assertEqual(validate_splunk_statefulset(FakeSplunk().statefulset()), [])

    def test_missing_persistent_volume_is_a_problem(self):
        statefulset = FakeSplunk().statefulset()
        statefulset["spec"]["volumeClaimTemplates"].pop()

        problems = validate_splunk_statefulset(statefulset)

        self.assertIn("/opt/splunk/var is not on a persistent volume", problems)

    def test_rolling_update_is_a_problem(self):
        statefulset = FakeSplunk().statefulset()
        statefulset["spec"]["updateStrategy"]["type"] = "RollingUpdate"

        self.assertTrue(validate_splunk_statefulset(statefulset))


class SplunkUpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backup_dir = directory.name

    def upgrade(self, fake, target=NEW):
        clock = Clock()
        return SplunkUpgrade(
            fake,
            target,
            self.backup_dir,
            step_timeout=60,
            poll_interval=10,
            sleep=clock.sleep,
            clock=clock,
        )

    def test_dry_run_changes_nothing(self):
        fake = FakeSplunk()

        report = self.upgrade(fake).run()

        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(fake.log, [])
        self.assertIs(report["changes_made"], False)

    def test_upgrade_backs_up_first_and_verifies(self):
        fake = FakeSplunk()

        report = self.upgrade(fake).run(execute=True)

        self.assertEqual(report["status"], "completed", msg=report["message"])
        self.assertEqual(fake.log[0], "backup")
        self.assertEqual(fake.version, NEW)
        self.assertEqual(report["events_after"], 50)

    def test_backup_is_private(self):
        report = self.upgrade(FakeSplunk()).run(execute=True)

        mode = stat.S_IMODE(os.stat(report["config_backup"]).st_mode)
        self.assertEqual(mode, 0o600)

    def test_invalid_backup_stops_before_any_change(self):
        for backup in (b"not an archive", backup_bytes(include_server_conf=False)):
            with self.subTest(backup=backup[:8]):
                fake = FakeSplunk()
                fake.backup = backup

                report = self.upgrade(fake).run(execute=True)

                self.assertEqual(report["status"], "failed")
                self.assertIs(report["changes_made"], False)
                self.assertEqual(fake.log, ["backup"])

    def test_bad_image_rolls_back(self):
        fake = FakeSplunk()
        fake.broken_tags = {"10.0.99"}

        report = self.upgrade(fake, target="10.0.99").run(execute=True)

        self.assertEqual(report["status"], "rolled_back", msg=report["message"])
        self.assertEqual(fake.version, OLD)
        self.assertEqual(fake.image, f"{REPOSITORY}:{OLD}")

    def test_failure_after_start_does_not_roll_back(self):
        fake = FakeSplunk()
        fake.lose_events = True

        report = self.upgrade(fake).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("Events fell", report["message"])
        self.assertIn("no downgrade path", report["message"])
        self.assertEqual(fake.image, f"{REPOSITORY}:{NEW}")

    def test_never_ready_times_out_without_rollback(self):
        fake = FakeSplunk()
        fake.never_ready = True

        report = self.upgrade(fake).run(execute=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("Timed out", report["message"])
        self.assertEqual(fake.image, f"{REPOSITORY}:{NEW}")

    def test_checks_block_the_upgrade(self):
        def red(fake):
            fake.health = "red"

        def full(fake):
            fake.disk = 90

        for change, word in ((red, "health"), (full, "disk")):
            with self.subTest(word=word):
                fake = FakeSplunk()
                change(fake)

                report = self.upgrade(fake).run(execute=True)

                self.assertEqual(report["status"], "blocked")
                self.assertIn(word, report["message"])
                self.assertEqual(fake.log, [])

    def test_unsupported_path_blocks(self):
        fake = FakeSplunk()

        report = self.upgrade(fake, target="10.6.1").run(execute=True)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(fake.log, [])

    def test_warning_needs_acceptance(self):
        fake = FakeSplunk()
        fake.health = "yellow"

        report = self.upgrade(fake).run(execute=True)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(fake.log, [])

        report = self.upgrade(fake).run(execute=True, accept_warnings=True)
        self.assertEqual(report["status"], "completed", msg=report["message"])

    def test_kubectl_failure_is_reported(self):
        fake = FakeSplunk()

        def broken():
            raise UpgradeError("kubectl get failed")

        fake.statefulset = broken

        report = self.upgrade(fake).run()

        self.assertEqual(report["status"], "failed")

    def test_invalid_api_response_is_reported(self):
        fake = FakeSplunk()
        fake.exec_in_pod = lambda *_args: "not json"

        report = self.upgrade(fake).run()

        self.assertEqual(report["status"], "failed")
        self.assertEqual(list(Path(self.backup_dir).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
