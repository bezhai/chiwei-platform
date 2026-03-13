#!/usr/bin/env bash
# PreToolUse hook: validate Bash commands.
# Defense-in-depth layer — catches dangerous operations even if settings.json
# pattern matching is bypassed (e.g. bash -c "kubectl apply ...", curl -s -XPOST ...).
#
# Exit codes:
#   0 = allow (pass through to settings.json rules)
#   2 = deny (hard block)

set -euo pipefail

# The tool input is passed as $1 (JSON with "command" field)
TOOL_INPUT="${1:-}"

if [[ -z "$TOOL_INPUT" ]]; then
  exit 0
fi

# Extract the command string from JSON input
COMMAND=$(echo "$TOOL_INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('command', ''))
except:
    print('')
" 2>/dev/null)

if [[ -z "$COMMAND" ]]; then
  exit 0
fi

# --- Rule 1: kubectl write + network bypass operations → hard deny ---
KUBECTL_WRITE_RE='kubectl\s+(apply|delete|edit|patch|create|exec|run|replace|scale|rollout|label|annotate|taint|drain|cordon|uncordon|cp|port-forward|proxy)\b'

if echo "$COMMAND" | grep -qPi "$KUBECTL_WRITE_RE"; then
  echo "BLOCKED: kubectl write/bypass operation detected. Use make commands or \$PAAS_API instead." >&2
  exit 2
fi

# --- Rule 2: curl write operations → hard deny ---
# Catches all flag variants: -X POST, -XPOST, --request POST, -s -X POST, etc.
# Only write methods are blocked; GET/HEAD pass through.
CURL_WRITE_RE='curl\b.*(-X\s*(POST|PUT|DELETE|PATCH)\b|-X(POST|PUT|DELETE|PATCH)\b|--request\s*(POST|PUT|DELETE|PATCH)\b)'

# Also catch implicit POST: curl with --data/--data-raw/-d but no explicit -X GET
CURL_DATA_RE='curl\b.*(\s-d\s|\s-d$|\s--data\s|\s--data-raw\s|\s--data-binary\s|\s--data-urlencode\s|\s-F\s|\s--form\s)'

if echo "$COMMAND" | grep -qPi "$CURL_WRITE_RE"; then
  echo "BLOCKED: curl write request detected. Use make commands (make deploy/release/undeploy/ops-query/...) instead of direct curl." >&2
  exit 2
fi

if echo "$COMMAND" | grep -qPi "$CURL_DATA_RE"; then
  # -d without explicit method = POST. Block it.
  # Exception: if there's also -X GET or --request GET, allow it (pass through).
  if ! echo "$COMMAND" | grep -qPi 'curl\b.*(-X\s*GET\b|-XGET\b|--request\s*GET\b)'; then
    echo "BLOCKED: curl with data payload (implicit POST) detected. Use make commands instead of direct curl." >&2
    exit 2
  fi
fi

# --- Rule 3: direct connection to internal services → hard deny ---
# Block curl/wget/httpie to internal addresses that bypass $PAAS_API.
# Allowed: $PAAS_API (env var reference is fine, the actual URL is handled by make).
# Blocked: K8s service DNS (.svc.cluster.local), pod IPs (10.x), localhost with service ports.
INTERNAL_RE='(curl|wget|http)\b.*(\.svc\.cluster\.local|\.prod\.svc|localhost:[0-9]|127\.0\.0\.1:[0-9]|10\.[0-9]+\.[0-9]+\.[0-9]+:[0-9])'

if echo "$COMMAND" | grep -qPi "$INTERNAL_RE"; then
  echo "BLOCKED: direct connection to internal service detected. All API access must go through \$PAAS_API. See CLAUDE.md network topology." >&2
  exit 2
fi

# Also block psql/mysql direct database connections
DB_CLIENT_RE='\b(psql|mysql|mongosh|redis-cli)\b'

if echo "$COMMAND" | grep -qPi "$DB_CLIENT_RE"; then
  echo "BLOCKED: direct database client connection detected. Use 'make ops-query' instead." >&2
  exit 2
fi

# All other commands: pass through (let settings.json handle allow/ask/deny)
exit 0
