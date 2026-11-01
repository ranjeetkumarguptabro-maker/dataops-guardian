#!/usr/bin/env bash
# End-to-end Splunk upgrade test: 9.4.17 -> 10.0.12, including a blocked path and a
# bad image that must roll back. Needs a cluster with about 4 GB of free memory.
set -Eeuo pipefail

TARGET=10.0.12
START=9.4.17
BAD=10.0.99
NS=splunk
OUT="${E2E_OUTPUT:-reports/e2e}"
# Backups contain secrets, so they stay out of the reports that CI uploads.
BACKUPS="$(mktemp -d)"
mkdir -p "$OUT"

upgrade() {
    dataops-guardian-splunk-upgrade --report-dir "$OUT" --backup-dir "$BACKUPS" "$@" \
        2>>"$OUT/splunk-upgrade.log"
}
field() { python3 -c "import json,sys;print(json.load(sys.stdin)['$1'])"; }
in_pod() { kubectl -n "$NS" exec splunk-0 -- sh -c "$1"; }
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
    echo "$output" >"$OUT/splunk-last.json"
    expect "$name" "$expected" "$(field status <<<"$output")" || {
        cat "$OUT/splunk-last.json"
        exit 1
    }
}

version() {
    in_pod 'curl -sk -m 30 -u "admin:$SPLUNK_PASSWORD" "https://localhost:8089/services/server/info?output_mode=json"' |
        python3 -c 'import json,sys;print(json.load(sys.stdin)["entry"][0]["content"]["version"])'
}

event_count() {
    in_pod 'curl -sk -m 60 -u "admin:$SPLUNK_PASSWORD" https://localhost:8089/services/search/jobs/export --data-urlencode "search=search index=main | stats count" -d output_mode=json' |
        python3 -c '
import json, sys
results = [json.loads(line)["result"] for line in sys.stdin if "\"result\"" in line]
print(results[-1]["count"])
'
}

wait_ready() {
    kubectl -n "$NS" wait pod/splunk-0 --for=condition=Ready --timeout=900s
}

echo "=== Deploy Splunk $START"
kubectl create namespace "$NS"
kubectl -n "$NS" create secret generic splunk-credentials \
    --from-literal=password="$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')" \
    --from-literal=hec-token="$(python3 -c 'import uuid;print(uuid.uuid4())')" >/dev/null
kubectl apply -f k8s/splunk/splunk.yaml
python3 -c 'import time;time.sleep(10)'
wait_ready
expect "starting version" "$START" "$(version)"

echo "=== Send 50 events"
python3 -c '
import json
print("".join(json.dumps({"event": {"n": i}, "sourcetype": "guardian:e2e"}) for i in range(50)))
' | kubectl -n "$NS" exec -i splunk-0 -- sh -c \
    'curl -fs -m 30 http://localhost:8088/services/collector/event -H "Authorization: Splunk $SPLUNK_HEC_TOKEN" --data-binary @-' >/dev/null

for attempt in $(seq 1 30); do
    [ "$(event_count)" = "50" ] && break
    python3 -c 'import time;time.sleep(5)'
done
expect "events searchable" 50 "$(event_count)"

echo "=== Unsafe requests must change nothing"
run_expect_status "unsupported path is blocked" blocked \
    --target-version 10.6.1 --execute --accept-warnings
run_expect_status "dry run" dry_run --target-version "$TARGET"
expect "version unchanged" "$START" "$(version)"

echo "=== A bad image must roll back"
run_expect_status "bad image rolls back" rolled_back \
    --target-version "$BAD" --execute --accept-warnings
wait_ready
expect "version after rollback" "$START" "$(version)"
expect "events after rollback" 50 "$(event_count)"

echo "=== Upgrade"
run_expect_status "upgrade" completed \
    --target-version "$TARGET" --execute --accept-warnings
expect "version after upgrade" "$TARGET" "$(version)"
expect "events after upgrade" 50 "$(event_count)"

echo "=== Backup"
BACKUP="$(field config_backup <"$OUT/splunk-last.json")"
tar tzf "$BACKUP" | grep -q '^etc/system/local/server.conf$' || fail "backup is incomplete"
expect "backup is private" 600 "$(stat -c '%a' "$BACKUP")"
echo "E2E PASSED"
