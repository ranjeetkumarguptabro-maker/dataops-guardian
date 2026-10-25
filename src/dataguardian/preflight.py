
"""Pre-upgrade validation for DataOps Guardian."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dataguardian.health import check_health

logger = logging.getLogger("dataguardian.preflight")


def run_preflight(
    health_url: str,
    timeout: float = 3.0,
    required_version: str | None = None,
) -> dict:
    """Validate upgrade prerequisites without changing the target system."""
    checks = []

    def record(name: str, passed: bool, message: str) -> None:
        checks.append({
            "name": name,
            "status": "passed" if passed else "failed",
            "message": message,
        })

    parsed = urlparse(health_url or "")
    valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    record(
        "health_url",
        valid_url,
        "Valid HTTP(S) URL" if valid_url else "A valid HTTP(S) URL is required",
    )

    valid_timeout = 0 < timeout <= 60
    record(
        "timeout",
        valid_timeout,
        "Timeout is within the allowed range"
        if valid_timeout
        else "Timeout must be greater than 0 and no more than 60 seconds",
    )

    version_ok = required_version is None or bool(required_version.strip())
    record(
        "required_version",
        version_ok,
        "Version configuration is valid"
        if version_ok
        else "Required version cannot be empty",
    )

    if valid_url and valid_timeout and version_ok:
        health = check_health(health_url, timeout)
        record(
            "service_health",
            health["status"] == "healthy",
            "Target endpoint is healthy"
            if health["status"] == "healthy"
            else f"Target endpoint is unhealthy: {health['error']}",
        )
    else:
        record(
            "service_health",
            False,
            "Skipped because configuration validation failed",
        )

    passed = all(check["status"] == "passed" for check in checks)

    return {
        "component": "dataguardian-preflight",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": health_url,
        "status": "passed" if passed else "failed",
        "checks": checks,
        "check_count": len(checks),
        "failed_count": sum(
            check["status"] == "failed" for check in checks
        ),
    }


def save_report(report: dict, output_dir: str = "reports") -> Path:
    """Write a preflight report to a timestamped JSON file."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = directory / f"preflight-{timestamp}.json"

    output_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate prerequisites before a platform upgrade."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("TARGET_HEALTH_URL"),
        help="Target health URL or TARGET_HEALTH_URL environment variable.",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--required-version",
        default=os.getenv("REQUIRED_VERSION"),
        help="Expected version for later upgrade validation.",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory in which to save the JSON report.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not args.url:
        parser.error("Provide --url or set TARGET_HEALTH_URL.")

    report = run_preflight(
        health_url=args.url,
        timeout=args.timeout,
        required_version=args.required_version,
    )

    print(json.dumps(report, indent=2))

    try:
        report_path = save_report(report, args.report_dir)
    except OSError as exc:
        logger.error("Could not save preflight report: %s", exc)
        return 2

    logger.info("Report saved to %s", report_path)
    logger.info(
        "Preflight completed: status=%s failed_count=%s",
        report["status"],
        report["failed_count"],
    )

    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
