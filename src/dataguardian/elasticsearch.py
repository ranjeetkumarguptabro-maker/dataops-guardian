
"""Utilities for discovering an Elasticsearch server version."""

import base64
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_elasticsearch_version(
    base_url: str,
    timeout: float = 3.0,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """Retrieve Elasticsearch's version using optional basic authentication."""

    username = (
        username if username is not None
        else os.getenv("ELASTICSEARCH_USERNAME")
    )
    password = (
        password if password is not None
        else os.getenv("ELASTICSEARCH_PASSWORD")
    )

    if (username is None) != (password is None):
        return {
            "status": "failed",
            "version": None,
            "message": "Both Elasticsearch username and password are required",
        }

    headers = {"Accept": "application/json"}

    if username is not None and password is not None:
        credentials = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"

    request = Request(
        base_url.rstrip("/") + "/",
        headers=headers,
        method="GET",
    )

    try:
        with urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Unexpected response structure")

        version_info = data.get("version", {})
        version = (
            version_info.get("number")
            if isinstance(version_info, dict)
            else None
        )

        if not isinstance(version, str) or not version.strip():
            return {
                "status": "failed",
                "version": None,
                "message": "Version number missing from Elasticsearch response",
            }

        return {
            "status": "passed",
            "version": version.strip(),
            "message": "Elasticsearch version retrieved successfully",
        }

    except HTTPError as exc:
        message = (
            "Elasticsearch authentication or authorization failed"
            if exc.code in (401, 403)
            else f"Elasticsearch returned HTTP {exc.code}"
        )
        return {
            "status": "failed",
            "version": None,
            "message": message,
        }

    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            message = "TLS verification or handshake failed"
        else:
            message = "Could not connect to Elasticsearch"

        return {
            "status": "failed",
            "version": None,
            "message": message,
        }

    except (TimeoutError, OSError):
        return {
            "status": "failed",
            "version": None,
            "message": "Connection timed out or failed",
        }

    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {
            "status": "failed",
            "version": None,
            "message": "Elasticsearch returned an invalid response",
        }
