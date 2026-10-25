"""Utilities for securely discovering an Elasticsearch server version."""

import base64
import json
import math
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def get_elasticsearch_version(
    base_url: str,
    timeout: float = 3.0,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """Retrieve Elasticsearch's version using optional Basic Authentication."""

    def failure(message: str) -> dict:
        return {
            "status": "failed",
            "version": None,
            "message": message,
        }

    # Validate inputs before making a network request.
    if not isinstance(base_url, str) or not base_url.strip():
        return failure("A valid Elasticsearch URL is required")

    try:
        parsed = urlparse(base_url.strip())

        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port == 0
        ):
            return failure("A valid Elasticsearch HTTP(S) URL is required")

    except (ValueError, AttributeError):
        return failure("A valid Elasticsearch HTTP(S) URL is required")

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 60
    ):
        return failure("Timeout must be greater than 0 and no more than 60 seconds")

    # Read credentials from explicit arguments or environment variables.
    if username is None:
        username = os.getenv("ELASTICSEARCH_USERNAME")
    if password is None:
        password = os.getenv("ELASTICSEARCH_PASSWORD")

    if (username is None) != (password is None):
        return failure("Both Elasticsearch username and password are required")

    if username is not None and (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not username
        or not password
    ):
        return failure("Elasticsearch credentials must not be empty")

    headers = {"Accept": "application/json"}

    if username is not None and password is not None:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {credentials}"

    request = Request(
        base_url.strip().rstrip("/") + "/",
        headers=headers,
        method="GET",
    )

    try:
        # urllib verifies TLS certificates by default. Keep verification enabled.
        with urlopen(
            request,
            timeout=float(timeout),
            context=ssl.create_default_context(),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, dict):
            return failure("Elasticsearch returned an invalid response")

        version_info = data.get("version")
        if not isinstance(version_info, dict):
            return failure("Version information missing from Elasticsearch response")

        version = version_info.get("number")
        if not isinstance(version, str) or not version.strip():
            return failure("Version number missing from Elasticsearch response")

        return {
            "status": "passed",
            "version": version.strip(),
            "message": "Elasticsearch version retrieved successfully",
        }

    except HTTPError as exc:
        if exc.code in (401, 403):
            return failure("Elasticsearch authentication or authorization failed")
        return failure(f"Elasticsearch returned HTTP {exc.code}")

    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            return failure("TLS verification or handshake failed")
        return failure("Could not connect to Elasticsearch")

    except (TimeoutError, OSError):
        return failure("Connection timed out or failed")

    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return failure("Elasticsearch returned an invalid response")
