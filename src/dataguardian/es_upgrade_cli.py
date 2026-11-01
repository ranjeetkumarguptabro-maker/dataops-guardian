"""Command-line interface for rolling Elasticsearch upgrades on Kubernetes."""

import argparse
import json
import logging

from dataguardian.es_upgrade import KubernetesCluster, RollingUpgrade
from dataguardian.splunk import publish_report
from dataguardian.upgrade_checks import save_report


def main(argv: list[str] | None = None) -> int:
    """Run a dry run by default; change the cluster only with --execute."""
    parser = argparse.ArgumentParser(
        description="Plan or run a rolling Elasticsearch upgrade on Kubernetes."
    )
    parser.add_argument("--namespace", default="elasticsearch")
    parser.add_argument("--statefulset", default="es")
    parser.add_argument(
        "--target-version",
        required=True,
        help="Version to upgrade to, for example 8.19.12",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Upgrade the cluster; without this flag only a plan is produced",
    )
    parser.add_argument(
        "--accept-warnings",
        action="store_true",
        help="Continue despite warnings such as a missing snapshot",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an interrupted upgrade to the same target version",
    )
    parser.add_argument(
        "--snapshot-repository",
        help="Take a snapshot in this registered repository before upgrading",
    )
    parser.add_argument(
        "--step-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for each step (default: 300)",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory for the JSON report",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    upgrade = RollingUpgrade(
        KubernetesCluster(args.namespace, args.statefulset),
        args.target_version,
        step_timeout=args.step_timeout,
        snapshot_repository=args.snapshot_repository,
        backup_dir=args.report_dir,
    )
    report = upgrade.run(
        execute=args.execute,
        accept_warnings=args.accept_warnings,
        resume=args.resume,
    )
    report["splunk"] = publish_report(report)
    report["report_path"] = save_report(report, args.report_dir, "es-upgrade")

    print(json.dumps(report, indent=2))

    return 0 if report["status"] in {"completed", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
