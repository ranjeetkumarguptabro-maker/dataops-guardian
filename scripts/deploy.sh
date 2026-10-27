#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="dataops-guardian"
CHART="helm/dataops-guardian"
NAMESPACE="default"
TIMEOUT="120s"

source "$(dirname "$0")/audit.sh"

audit_log "deployment_started" "release=$RELEASE namespace=$NAMESPACE"

if ! helm lint "$CHART" ||
   ! helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" >/dev/null; then
    audit_log "validation_failed" "chart=$CHART"
    exit 1
fi

if ! kubectl get deployment "$RELEASE" -n "$NAMESPACE" >/dev/null; then
    audit_log "precheck_failed" "deployment=$RELEASE"
    exit 1
fi

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

audit_log "upgrade_started" "previous_revision=$PREVIOUS_REVISION"

if helm upgrade "$RELEASE" "$CHART" \
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
