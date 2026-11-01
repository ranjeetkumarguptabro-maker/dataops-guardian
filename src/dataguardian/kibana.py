"""Import the DataOps Guardian dashboard into Kibana."""

import argparse
import json
import os
import ssl
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_FILE = "kibana/guardian-dashboard.ndjson"


def import_dashboard(kibana_url: str, path: str, timeout: float = 30.0) -> dict:
    """Upload saved objects to Kibana, replacing earlier copies of them."""
    try:
        content = Path(path).read_bytes()
    except OSError:
        return {"status": "failed", "message": f"Could not read {path}"}

    boundary = uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; '
            'filename="dashboard.ndjson"\r\n'
            "Content-Type: application/ndjson\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "kbn-xsrf": "true",
    }
    api_key = os.getenv("ELASTICSEARCH_API_KEY")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    request = Request(
        kibana_url.rstrip("/") + "/api/saved_objects/_import?overwrite=true",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        # urllib verifies TLS certificates by default. Keep verification enabled.
        with urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            return {"status": "failed", "message": "Kibana rejected the credentials"}
        return {"status": "failed", "message": f"Kibana returned HTTP {exc.code}"}
    except (URLError, TimeoutError, OSError):
        return {"status": "failed", "message": "Could not connect to Kibana"}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "failed", "message": "Kibana returned an invalid response"}

    if not result.get("success"):
        return {
            "status": "failed",
            "message": "Kibana refused some objects",
            "errors": result.get("errors", []),
        }
    return {
        "status": "passed",
        "message": f"Imported {result.get('successCount', 0)} objects",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import the DataOps Guardian dashboard into Kibana."
    )
    parser.add_argument("--url", required=True, help="Kibana URL")
    parser.add_argument("--file", default=DEFAULT_FILE)
    args = parser.parse_args(argv)

    result = import_dashboard(args.url, args.file)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
