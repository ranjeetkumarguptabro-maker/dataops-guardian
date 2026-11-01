#!/usr/bin/env bash
# End-to-end rolling upgrade test: 8.17.4 -> 8.19.12 -> 9.5.5, including the
# failures the tool must handle. Needs a cluster with about 6 GB of free memory.
set -Eeuo pipefail

FIRST=8.19.12
FINAL=9.5.5
BAD=8.19.99
NS=elasticsearch
OUT="${E2E_OUTPUT:-reports/e2e}"
mkdir -p "$OUT"

upgrade() { dataops-guardian-es-upgrade --report-dir "$OUT" "$@" 2>>"$OUT/upgrade.log"; }
field() { python3 -c "import json,sys;print(json.load(sys.stdin)['$1'])"; }
es() { kubectl -n "$NS" exec "$1" -- curl -s -m 20 "${@:2}"; }
fail() { echo "E2E FAILED: $*" >&2; exit 1; }

expect() {
    local name="$1" expected="$2" actual="$3"
    [ "$actual" = "$expected" ] || fail "$name: expected '$expected', got '$actual'"
    echo "PASS $name: $actual"
}

run_expect_status() {
    local name="$1" expected="$2" output
    shift 2
    output="$(upgrade "$@" || true)"
    expect "$name" "$expected" "$(field status <<<"$output")"
}

template_image() {
    kubectl -n "$NS" get statefulset es \
        -o jsonpath='{.spec.template.spec.containers[0].image}'
}

# Pick a pod that can answer: the bad-image step leaves one pod unable to start.
ready_pod() {
    kubectl -n "$NS" get pods -l app=elasticsearch -o json | python3 -c '
import json, sys
for pod in json.load(sys.stdin)["items"]:
    if any(c["type"] == "Ready" and c["status"] == "True"
           for c in pod["status"].get("conditions", [])):
        print(pod["metadata"]["name"])
        break
'
}

run_with_probe() {
    local version="$1" index="$2" output probe_pid
    shift 2
    kubectl -n "$NS" exec -i es-probe -- python - "$version" "$index" \
        <scripts/es_probe.py >"$OUT/probe-$index.json" &
    probe_pid=$!
    output="$(upgrade --target-version "$version" --execute --accept-warnings "$@" ||
        true)"
    echo "$output" >"$OUT/last-report.json"
    expect "upgrade to $version" completed "$(field status <<<"$output")"
    wait "$probe_pid" || fail "probe for $version did not finish"
    echo "Client view during upgrade to $version: $(cat "$OUT/probe-$index.json")"

    local pod acknowledged stored
    pod="$(ready_pod)"
    acknowledged="$(field write_ok <"$OUT/probe-$index.json")"
    es "$pod" -XPOST "localhost:9200/$index/_refresh" >/dev/null
    stored="$(es "$pod" "localhost:9200/$index/_count" | field count)"
    [ "$stored" -ge "$acknowledged" ] ||
        fail "$index lost writes: $acknowledged acknowledged, $stored stored"
    echo "PASS acknowledged writes kept: $acknowledged acknowledged, $stored stored"
    expect "seed documents after $version" 1000 \
        "$(es "$pod" localhost:9200/upgrade-test/_count | field count)"
}

echo "=== Deploy 3 nodes on 8.17.4"
kubectl apply -f k8s/elasticsearch/elasticsearch.yaml
python3 -c 'import time;time.sleep(10)'
kubectl -n "$NS" wait pod -l app=elasticsearch --for=condition=Ready --timeout=420s
kubectl -n "$NS" run es-probe --image=python:3.12-slim --restart=Never \
    --command -- sleep 7200
kubectl -n "$NS" wait pod/es-probe --for=condition=Ready --timeout=180s

echo "=== Seed 1000 documents"
es es-0 -XPUT localhost:9200/upgrade-test -H 'content-type: application/json' \
    -d '{"settings":{"number_of_shards":3,"number_of_replicas":1}}'
echo
python3 -c '
import json
for i in range(1000):
    print(json.dumps({"index": {"_id": str(i)}}))
    print(json.dumps({"n": i}))
' | kubectl -n "$NS" exec -i es-0 -- curl -s -XPOST \
    'localhost:9200/upgrade-test/_bulk?refresh=true' \
    -H 'content-type: application/x-ndjson' --data-binary @- >/dev/null
expect "seed documents" 1000 "$(es es-0 localhost:9200/upgrade-test/_count | field count)"
es es-0 'localhost:9200/_cluster/health?wait_for_status=green&timeout=120s' >/dev/null

ORIGINAL_IMAGE="$(template_image)"

echo "=== Unsafe requests must change nothing"
run_expect_status "skipping 8.19 is blocked" blocked \
    --target-version "$FINAL" --execute --accept-warnings
run_expect_status "dry run" dry_run --target-version "$FIRST"
run_expect_status "warnings need acceptance" blocked \
    --target-version "$FIRST" --execute
expect "image unchanged" "$ORIGINAL_IMAGE" "$(template_image)"

echo "=== A bad image must stop after one node, without data loss"
run_expect_status "bad image stops the upgrade" failed \
    --target-version "$BAD" --execute --accept-warnings --step-timeout 120
POD="$(ready_pod)"
expect "seed documents after failure" 1000 \
    "$(es "$POD" localhost:9200/upgrade-test/_count | field count)"

echo "=== Resume to a good version with clients running"
run_with_probe "$FIRST" probe-writes-first --resume

echo "=== Major upgrade with clients running, after a snapshot"
POD="$(ready_pod)"
es "$POD" -XPUT localhost:9200/_snapshot/guardian -H 'content-type: application/json' \
    -d '{"type":"fs","settings":{"location":"/snapshots"}}'
echo
run_with_probe "$FINAL" probe-writes-final --snapshot-repository guardian

echo "=== The snapshot taken before the upgrade must restore on the new version"
SNAPSHOT="$(field snapshot <"$OUT/last-report.json")"
POD="$(ready_pod)"
es "$POD" -XPOST \
    "localhost:9200/_snapshot/guardian/$SNAPSHOT/_restore?wait_for_completion=true" \
    -H 'content-type: application/json' -d '{"indices":"upgrade-test",
    "rename_pattern":"(.+)","rename_replacement":"restored-$1"}' >/dev/null
es "$POD" -XPOST localhost:9200/restored-upgrade-test/_refresh >/dev/null
expect "documents restored from snapshot" 1000 \
    "$(es "$POD" localhost:9200/restored-upgrade-test/_count | field count)"

echo "=== Final state"
POD="$(ready_pod)"
es "$POD" 'localhost:9200/_cat/nodes?v&h=name,version,master'
expect "cluster health" green "$(es "$POD" localhost:9200/_cluster/health | field status)"
versions="$(es "$POD" 'localhost:9200/_cat/nodes?h=version' | sort -u | tr -d ' \n')"
expect "all nodes on target" "$FINAL" "$versions"
echo "E2E PASSED"
