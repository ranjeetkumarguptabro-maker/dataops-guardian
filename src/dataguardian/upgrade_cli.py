"""Command-line interface for Elasticsearch upgrade readiness checks."""

import argparse
import json

from dataguardian.upgrade_checks import run_upgrade_checks, save_report


def main(argv: list[str] | None = None) -> int:
    """Run the checks and exit non-zero when the upgrade is blocked."""
    parser = argparse.ArgumentParser(
        description="Check whether Elasticsearch is ready to upgrade. Read-only."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Elasticsearch URL, for example http://localhost:9200",
    )
    parser.add_argument(
        "--target-version",
        required=True,
        help="Version to upgrade to, for example 8.19.0",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Request timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory for the JSON report",
    )

    args = parser.parse_args(argv)

    report = run_upgrade_checks(args.url, args.target_version, timeout=args.timeout)
    report["report_path"] = save_report(report, args.report_dir)

    print(json.dumps(report, indent=2))

    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
