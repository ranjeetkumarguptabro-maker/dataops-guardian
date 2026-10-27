#!/usr/bin/env bash

set -Eeuo pipefail

LOG_DIR="${LOG_DIR:-logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/maintenance.log}"

mkdir -p "$LOG_DIR"

audit_log() {
    local event="$1"
    shift

    local timestamp
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    local message="$*"
    message="${message//$'\n'/ }"

    printf '%s event=%s details=%s\n' \
        "$timestamp" "$event" "$message" \
        | tee -a "$LOG_FILE"
}
