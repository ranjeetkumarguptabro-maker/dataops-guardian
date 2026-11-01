"""Read and write once a second until the cluster reaches a version, then report.

Runs inside the cluster during an upgrade to measure what clients would see.
Usage: python es_probe.py TARGET_VERSION WRITE_INDEX
"""

import json
import sys
import time
import urllib.request

ES = "http://elasticsearch.elasticsearch:9200"
MAX_SECONDS = 1500
target, write_index = sys.argv[1], sys.argv[2]


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(
        ES + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


stats = {"read_ok": 0, "read_fail": 0, "write_ok": 0, "write_fail": 0}
streak = longest_streak = sequence = 0
start = time.time()

while time.time() - start < MAX_SECONDS:
    tick = time.time()
    healthy = True
    try:
        call("/upgrade-test/_count")
        stats["read_ok"] += 1
    except Exception:
        stats["read_fail"] += 1
        healthy = False
    try:
        sequence += 1
        call(f"/{write_index}/_doc/{sequence}", "PUT", {"at": tick})
        stats["write_ok"] += 1
    except Exception:
        stats["write_fail"] += 1
        healthy = False

    streak = 0 if healthy else streak + 1
    longest_streak = max(longest_streak, streak)

    try:
        versions = {n["version"] for n in call("/_cat/nodes?format=json&h=version")}
        status = call("/_cluster/health")["status"]
        if versions == {target} and status == "green":
            break
    except Exception:
        pass
    time.sleep(max(0, 1 - (time.time() - tick)))

stats["longest_failed_streak_seconds"] = longest_streak
stats["seconds"] = round(time.time() - start)
print(json.dumps(stats))
