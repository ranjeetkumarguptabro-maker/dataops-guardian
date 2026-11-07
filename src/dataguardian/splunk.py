"""Send DataOps Guardian reports to Splunk through the HTTP Event Collector."""

import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("dataguardian.splunk")

SOURCETYPE = "dataguardian:report"
SOURCE = "dataops-guardian"


def _request(url: str, timeout: float, token: str | None = None, body=None) -> dict:
    """Call Splunk and return a status result; tokens never appear in results."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"status": "failed", "message": "A valid Splunk URL is required"}

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Splunk {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    try:
        # urllib verifies TLS certificates by default. Keep verification enabled.
        with urlopen(
            Request(url, data=data, headers=headers, method="POST" if data else "GET"),
            timeout=timeout,
            context=ssl.create_default_context(),
        ):
            return {"status": "passed", "message": "Splunk accepted the request"}
    except HTTPError as exc:
        if exc.code in (401, 403):
            message = "Splunk rejected the HEC token"
        else:
            message = f"Splunk returned HTTP {exc.code}"
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            message = "TLS verification or handshake failed"
        else:
            message = "Could not connect to Splunk"
    except (TimeoutError, OSError):
        message = "Connection to Splunk timed out or failed"
    return {"status": "failed", "message": message}


def check_splunk_health(hec_url: str, timeout: float = 5.0) -> dict:
    """Check that the HTTP Event Collector is ready to receive events."""
    result = _request(hec_url.rstrip("/") + "/services/collector/health", timeout)
    return {"name": "splunk_hec", **result}


def send_event(
    hec_url: str,
    token: str,
    event: dict,
    timeout: float = 5.0,
    timestamp: float | None = None,
) -> dict:
    """Send one event to the HTTP Event Collector."""
    return _request(
        hec_url.rstrip("/") + "/services/collector/event",
        timeout,
        token=token,
        body={
            "time": timestamp if timestamp is not None else time.time(),
            "source": SOURCE,
            "sourcetype": SOURCETYPE,
            "event": event,
        },
    )


def publish_report(report: dict) -> dict:
    """Send a report to Splunk when configured.

    A Splunk outage must never change the outcome of the maintenance work being
    reported, so failures are returned rather than raised.
    """
    url = os.getenv("SPLUNK_HEC_URL")
    token = os.getenv("SPLUNK_HEC_TOKEN")
    if not url or not token:
        return {"status": "skipped", "message": "Splunk is not configured"}

    event = dict(report)
    status = str(event.get("status", "unknown")).lower()

    severity = (
        "ERROR"
        if status in {"failed", "blocked", "alert", "unhealthy"}
        else "WARN"
        if status in {"warning", "degraded"}
        else "INFO"
    )

    timestamp = (
        event.get("generated_at") or event.get("checked_at") or event.get("timestamp")
    )

    if timestamp:
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            event_time = parsed.timestamp()
        except (ValueError, TypeError, OverflowError):
            event_time = time.time()
    else:
        event_time = time.time()

    event.setdefault("service", "dataops-guardian")
    event.setdefault("event_type", event.get("component", "maintenance_report"))
    event.setdefault("severity", severity)
    environment = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "unknown"))
    event.setdefault("environment", environment)

    result = send_event(url, token, event, timestamp=event_time)
    if result["status"] != "passed":
        logger.warning("Could not send report to Splunk: %s", result["message"])
    return result
