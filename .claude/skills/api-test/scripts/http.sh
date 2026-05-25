#!/usr/bin/env bash
# Small HTTP helper for common JSON API calls.
#
# It is not meant to replace curl. For unsupported cases, use curl directly.
#
# Usage:
#   http.sh [--timeout SEC] [--raw] METHOD URL [BODY] [HEADER...]
#   http.sh [--timeout SEC] [--raw] METHOD URL --data <BODY> [HEADER...]
#   http.sh [--timeout SEC] [--raw] METHOD URL --data-binary @file [HEADER...]
#   http.sh --curl <curl args...>
#
# Examples:
#   http.sh GET "$PAAS_API/health"
#   http.sh --timeout 120 POST "$URL" '{"a":1}' "X-API-Key: $TOKEN"
#   http.sh PUT "$URL" @/tmp/body.json "Content-Type: application/json"
#   http.sh --curl -v --max-time 300 -H "X-API-Key: $TOKEN" "$URL"

set -uo pipefail

usage() {
  cat >&2 <<'USAGE'
http.sh [--timeout SEC] [--raw] METHOD URL [BODY] [HEADER...]
http.sh [--timeout SEC] [--raw] METHOD URL --data <BODY> [HEADER...]
http.sh [--timeout SEC] [--raw] METHOD URL --data-binary @file [HEADER...]
http.sh --curl <curl args...>

Common:
  http.sh GET "$PAAS_API/health"
  http.sh --timeout 120 POST "$URL" '{"a":1}' "X-API-Key: $TOKEN"
  http.sh PUT "$URL" @/tmp/body.json "Content-Type: application/json"
  http.sh --curl -v --max-time 300 -H "X-API-Key: $TOKEN" "$URL"
USAGE
  exit 2
}

if [[ $# -lt 1 ]]; then
  usage
fi

# Escape hatch: pass arguments to curl unchanged.
if [[ "${1:-}" == "--curl" ]]; then
  shift
  exec curl "$@"
fi

TIMEOUT="${HTTP_TIMEOUT:-60}"
RAW=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      [[ $# -ge 2 ]] || usage
      TIMEOUT="$2"
      shift 2
      ;;
    --raw)
      RAW=1
      shift
      ;;
    --help|-h)
      usage
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage
      ;;
    *)
      break
      ;;
  esac
done

[[ $# -ge 2 ]] || usage

METHOD="$1"
URL="$2"
shift 2

BODY=""
CURL_ARGS=(-sS --max-time "$TIMEOUT")

# Non-GET methods may take a JSON body or @file body as the first argument.
# Plain/non-JSON bodies should be passed with --data or --data-binary.
if [[ "$METHOD" != "GET" && $# -gt 0 ]]; then
  case "$1" in
    --data)
      [[ $# -ge 2 ]] || usage
      CURL_ARGS+=(-d "$2")
      shift 2
      ;;
    --data-binary)
      [[ $# -ge 2 ]] || usage
      CURL_ARGS+=(--data-binary "$2")
      shift 2
      ;;
    \{*|\[*)
      BODY="$1"
      CURL_ARGS+=(-H "Content-Type: application/json" -d "$BODY")
      shift
      ;;
    @*)
      BODY="$1"
      CURL_ARGS+=(--data-binary "$BODY")
      shift
      ;;
  esac
fi

while [[ $# -gt 0 ]]; do
  CURL_ARGS+=(-H "$1")
  shift
done

if [[ "$RAW" == "1" ]]; then
  exec curl "${CURL_ARGS[@]}" -X "$METHOD" "$URL"
fi

TMP_BODY="$(mktemp /tmp/http_body.XXXXXX)"
TMP_ERR="$(mktemp /tmp/http_err.XXXXXX)"
trap 'rm -f "$TMP_BODY" "$TMP_ERR"' EXIT

HTTP_CODE=$(curl "${CURL_ARGS[@]}" -X "$METHOD" -o "$TMP_BODY" -w "%{http_code}" "$URL" 2>"$TMP_ERR")
CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
  CURL_ERR=$(cat "$TMP_ERR" 2>/dev/null || echo "curl failed")
  python3 - "$CURL_EXIT" "$CURL_ERR" <<'PY'
import json
import sys
print(json.dumps({"status": 0, "error": f"curl exit {sys.argv[1]}: {sys.argv[2]}"}, ensure_ascii=False))
PY
  exit 0
fi

python3 - "$HTTP_CODE" "$TMP_BODY" <<'PY' || echo "{\"status\":$HTTP_CODE,\"body\":\"(parse error)\"}"
import json
import sys

status = int(sys.argv[1]) if sys.argv[1].isdigit() else 0
with open(sys.argv[2], "r", encoding="utf-8", errors="replace") as f:
    raw = f.read()

try:
    body = json.loads(raw)
except Exception:
    body = raw

print(json.dumps({"status": status, "body": body}, ensure_ascii=False))
PY
