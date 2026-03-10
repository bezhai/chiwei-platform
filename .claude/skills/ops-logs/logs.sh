#!/usr/bin/env bash
# Fetch logs for a service deployment.
# Usage: logs.sh <app> [lane] [lines]
#   app:   required, service name
#   lane:  optional, default "prod"
#   lines: optional, default 100

APP="${1:?用法: logs.sh <app> [lane] [lines]}"
LANE="${2:-prod}"
LINES="${3:-100}"

# Try {app}-{lane} first, fallback to {app}
kubectl logs "deploy/${APP}-${LANE}" -n prod --tail="${LINES}" --timestamps 2>/dev/null \
  || kubectl logs "deploy/${APP}" -n prod --tail="${LINES}" --timestamps 2>&1
