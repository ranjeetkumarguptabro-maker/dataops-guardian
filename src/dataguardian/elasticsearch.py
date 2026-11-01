"""Utilities for securely discovering an Elasticsearch server version."""

import base64
import json
import math
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ElasticsearchError(Exception):
    """A request to Elasticsearch could not be completed safely."""


def fetch_json(
    base_url: str,
    path: str = "/",
    timeout: float = 3.0,
    username: str | None = None,
    password: str | None = None,
):
    """Send a read-only GET request to Elasticsearch and return the parsed JSON."""
    return request_json(base_url, path, timeout, username, password)


def request_json(
    base_url: str,
    path: str = "/",
    timeout: float = 3.0,
    username: str | None = None,
    password: str | None = None,
    method: str = "GET",
    body: dict | None = None,
):
    """Send a request to Elasticsearch and return the parsed JSON response."""
    # Validate inputs before making a network request.
    if not isinstance(base_url, str) or not base_url.strip():
        raise ElasticsearchError("A valid Elasticsearch URL is required")

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
            raise ElasticsearchError("A valid Elasticsearch HTTP(S) URL is required")

    except (ValueError, AttributeError) as exc:
        raise ElasticsearchError(
            "A valid Elasticsearch HTTP(S) URL is required"
        ) from exc

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 60
    ):
        raise ElasticsearchError(
            "Timeout must be greater than 0 and no more than 60 seconds"
        )

    # Read credentials from explicit arguments or environment variables.
    if username is None:
        username = os.getenv("ELASTICSEARCH_USERNAME")
    if password is None:
        password = os.getenv("ELASTICSEARCH_PASSWORD")

    if (username is None) != (password is None):
        raise ElasticsearchError(
            "Both Elasticsearch username and password are required"
        )

    if username is not None and (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not username
        or not password
    ):
        raise ElasticsearchError("Elasticsearch credentials must not be empty")

    headers = {"Accept": "application/json"}

    # An API key is the only option on Elastic Cloud Serverless, so it comes first.
    api_key = os.getenv("ELASTICSEARCH_API_KEY")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    elif username is not None and password is not None:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {credentials}"

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        base_url.strip().rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        # urllib verifies TLS certificates by default. Keep verification enabled.
        with urlopen(
            request,
            timeout=float(timeout),
            context=ssl.create_default_context(),
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ElasticsearchError(
                "Elasticsearch authentication or authorization failed"
            ) from exc
        raise ElasticsearchError(f"Elasticsearch returned HTTP {exc.code}") from exc

    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise ElasticsearchError("TLS verification or handshake failed") from exc
        raise ElasticsearchError("Could not connect to Elasticsearch") from exc

    except (TimeoutError, OSError) as exc:
        raise ElasticsearchError("Connection timed out or failed") from exc

    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ElasticsearchError("Elasticsearch returned an invalid response") from exc


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

    try:
        data = fetch_json(base_url, "/", timeout, username, password)
    except ElasticsearchError as exc:
        return failure(str(exc))

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
