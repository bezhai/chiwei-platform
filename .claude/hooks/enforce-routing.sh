#!/usr/bin/env bash
# PreToolUse hook — SECURITY layer only:
#
#   SECURITY layer (unconditional, main session AND subagent):
#     kubectl write / curl write / direct internal connection / DB client
#     -> hard deny. Migrated from the old validate-bash.sh, rewritten to
#     the official stdin-JSON contract. This layer is unconditional: it
#     never inspects the caller (main session vs subagent) and never exempts.
#     Only Bash commands are inspected; all other tools pass through.
#
# The former ROUTE layer (main-session-only repo-file read/write interception
# + Bash orchestration allowlist) was removed 2026-05-22: subagent delegation
# is now a judgment-based recommendation in CLAUDE.md, not a mechanical block.
# The agent_id main/subagent distinction is gone with it — SECURITY is
# unconditional, so it never needed to know the caller.
#
# Exit codes: 0 = allow, 2 = deny (stderr fed back to Claude).

set -uo pipefail

PAYLOAD="$(cat 2>/dev/null || true)"

if [[ -z "${PAYLOAD//[[:space:]]/}" ]]; then
  exit 0
fi

# Parse the Bash command (the only field the security layer needs).
# Bad-JSON contract: fail-OPEN with an empty command (nothing to inspect).
COMMAND="$(printf '%s' "$PAYLOAD" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
ti = d.get("tool_input", {})
cmd = ti.get("command", "") if isinstance(ti, dict) else ""
print(cmd if isinstance(cmd, str) else "")
' 2>/dev/null)" || COMMAND=""

# ============================================================
# SECURITY LAYER — unconditional (main AND subagent)
# Only inspects Bash commands.
# ============================================================
if [[ -n "$COMMAND" ]]; then

  KUBECTL_WRITE_RE='kubectl\s+(apply|delete|edit|patch|create|exec|run|replace|scale|rollout|label|annotate|taint|drain|cordon|uncordon|cp|port-forward|proxy)\b'
  if echo "$COMMAND" | grep -qPi "$KUBECTL_WRITE_RE"; then
    echo "BLOCKED (security): kubectl write/bypass operation detected. Use make commands or \$PAAS_API instead." >&2
    exit 2
  fi

  # Cheap equivalence patch (T3): also catch `--request=POST` (equals form)
  # alongside the space form. Deliberately NOT chasing shell-token obfuscation
  # like kube""ctl — .claude/settings.json permission deny/ask is the
  # defense-in-depth backstop for that.
  CURL_WRITE_RE='curl\b.*(-X\s*(POST|PUT|DELETE|PATCH)\b|-X(POST|PUT|DELETE|PATCH)\b|--request[=\s]*(POST|PUT|DELETE|PATCH)\b)'
  if echo "$COMMAND" | grep -qPi "$CURL_WRITE_RE"; then
    echo "BLOCKED (security): curl write request detected. Use make commands instead of direct curl." >&2
    exit 2
  fi

  # `-d` glued straight to its value with no space (`-dfoo`, `-d@file`) is an
  # implicit POST just like `-d foo`; the old regex required a space after -d.
  CURL_DATA_RE='curl\b.*(\s-d\s|\s-d$|\s-d\S|\s--data\s|\s--data-raw\s|\s--data-binary\s|\s--data-urlencode\s|\s-F\s|\s--form\s)'
  if echo "$COMMAND" | grep -qPi "$CURL_DATA_RE"; then
    if ! echo "$COMMAND" | grep -qPi 'curl\b.*(-X\s*GET\b|-XGET\b|--request\s*GET\b)'; then
      echo "BLOCKED (security): curl with data payload (implicit POST) detected. Use make commands instead of direct curl." >&2
      exit 2
    fi
  fi

  INTERNAL_RE='(curl|wget|http)\b.*(\.svc\.cluster\.local|\.prod\.svc|localhost:[0-9]|127\.0\.0\.1:[0-9]|10\.[0-9]+\.[0-9]+\.[0-9]+:[0-9])'
  if echo "$COMMAND" | grep -qPi "$INTERNAL_RE"; then
    echo "BLOCKED (security): direct connection to internal service detected. All API access must go through \$PAAS_API." >&2
    exit 2
  fi

  DB_CLIENT_RE='\b(psql|mysql|mongosh|redis-cli)\b'
  if echo "$COMMAND" | grep -qPi "$DB_CLIENT_RE"; then
    echo "BLOCKED (security): direct database client connection detected. Use 'make ops-query' instead." >&2
    exit 2
  fi
fi

exit 0
