#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="dataops-guardian"
CHART="${CHART:-helm/dataops-guardian}"
NAMESPACE="default"
TIMEOUT="${TIMEOUT:-120s}"

source "$(dirname "$0")/audit.sh"

audit_log "deployment_started" "release=$RELEASE namespace=$NAMESPACE"

if ! helm lint "$CHART" ||
   ! helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" >/dev/null; then
    audit_log "validation_failed" "chart=$CHART"
    exit 1
fi

if ! kubectl cluster-info --request-timeout=15s >/dev/null; then
    audit_log "precheck_failed" "cluster_unreachable"
    exit 1
fi

# A first install has no release history, so there is nothing to roll back to.
PREVIOUS_REVISION=0
if helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
    PREVIOUS_REVISION="$(
      helm history "$RELEASE" -n "$NAMESPACE" -o json |
      python3 -c '
import json, sys
revisions = json.load(sys.stdin)
successful = [
    int(item["revision"])
    for item in revisions
    if item.get("status") == "deployed"
]
print(max(successful) if successful else 0)
'
    )"
fi

audit_log "upgrade_started" "previous_revision=$PREVIOUS_REVISION"

if helm upgrade --install "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" --wait --timeout "$TIMEOUT"; then

    if kubectl rollout status "deployment/$RELEASE" \
        -n "$NAMESPACE" --timeout="$TIMEOUT"; then
        CURRENT_REVISION="$(helm list -n "$NAMESPACE" -o json |
          python3 -c '
import json, sys
for item in json.load(sys.stdin):
    if item["name"] == "dataops-guardian":
        print(item["revision"])
        break
')"
        audit_log "deployment_succeeded" "revision=$CURRENT_REVISION"
        exit 0
    fi
fi

audit_log "upgrade_failed" "attempting_rollback_to=$PREVIOUS_REVISION"

if [ "$PREVIOUS_REVISION" -gt 0 ] &&
   helm rollback "$RELEASE" "$PREVIOUS_REVISION" \
       --namespace "$NAMESPACE" --wait --timeout "$TIMEOUT"; then
    if kubectl rollout status "deployment/$RELEASE" \
        -n "$NAMESPACE" --timeout="$TIMEOUT"; then
        audit_log "rollback_succeeded" "revision=$PREVIOUS_REVISION"
        exit 1
    fi
fi

audit_log "rollback_failed" "manual_investigation_required"
exit 1
