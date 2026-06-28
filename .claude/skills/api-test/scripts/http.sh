#!/usr/bin/env bash
# Small HTTP helper for this project's JSON APIs.
#
# It is not meant to replace curl. For unsupported cases, use curl directly
# (or the --curl escape hatch below).
#
# The point of this helper over raw curl: you write a *path*, it figures out
# the rest. A path that starts with "/" is resolved against $PAAS_API, and the
# one secret ($PAAS_TOKEN) is injected automatically — token choice never
# reaches the caller. Both surfaces accept it (the dashboard takes PAAS_TOKEN
# too), so /dashboard/... and /api/paas/... use the same key:
#   /dashboard/... -> X-API-Key: $PAAS_TOKEN  (audited dashboard entry)
#   /api/paas/...  -> X-API-Key: $PAAS_TOKEN  (direct paas-engine admin)
# A full http(s):// URL is used verbatim and never gets an auto token, so
# existing callers that pass full URLs + explicit headers are unaffected.
#
# Usage:
#   http.sh [opts] METHOD URL_OR_PATH [BODY] [HEADER...]
#   http.sh [opts] METHOD URL_OR_PATH --data <BODY> [HEADER...]
#   http.sh [opts] METHOD URL_OR_PATH --data-binary @file [HEADER...]
#   http.sh --curl <curl args...>          # raw passthrough to curl
#
# opts:
#   --timeout SEC   request timeout (default 60, or $HTTP_TIMEOUT)
#   --lane LANE     add "x-lane: LANE" (default $HTTP_LANE)
#   --no-auth       do not auto-inject any token
#   --jq FILTER     run `jq -r FILTER` over the raw response body (not the
#                   {status,ms,body} wrapper) and print only that
#   --expect CODE   exit nonzero if the HTTP status != CODE
#   --save FILE     also write the raw response body to FILE
#   --raw           stream the raw body (no JSON wrapping, no timing)
#
# Examples:
#   http.sh GET /dashboard/api/ops/services             # auto CC token + base
#   http.sh GET /api/paas/apps/agent-service/resolved-config?lane=prod
#   http.sh --lane ppe-x GET /api/paas/apps/agent-service/pods
#   http.sh POST /api/paas/apps/x '{"a":1}'
#   http.sh --jq '.body.version' GET /api/paas/apps/x
#   http.sh --expect 200 GET /dashboard/api/health
#   http.sh --curl -v --max-time 300 -H "X-API-Key: $TOKEN" "$URL"

set -uo pipefail

usage() {
  sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d;s/^# \{0,1\}//' >&2
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
LANE="${HTTP_LANE:-}"
RAW=0
NO_AUTH=0
JQ_FILTER=""
EXPECT=""
SAVE=""

# Options may appear anywhere (before or after METHOD/URL) — tacking --jq or
# --lane onto the end of a call is the natural habit. Pull options out of the
# stream and leave the positionals (METHOD, URL, body, headers) in POSITIONALS.
POSITIONALS=()
END_OPTS=0
while [[ $# -gt 0 ]]; do
  if [[ "$END_OPTS" -eq 1 ]]; then POSITIONALS+=("$1"); shift; continue; fi
  case "$1" in
    --timeout) [[ $# -ge 2 ]] || usage; TIMEOUT="$2"; shift 2 ;;
    --lane)    [[ $# -ge 2 ]] || usage; LANE="$2";    shift 2 ;;
    --jq)      [[ $# -ge 2 ]] || usage; JQ_FILTER="$2"; shift 2 ;;
    --expect)  [[ $# -ge 2 ]] || usage; EXPECT="$2";  shift 2 ;;
    --save)    [[ $# -ge 2 ]] || usage; SAVE="$2";    shift 2 ;;
    --raw)     RAW=1; shift ;;
    --no-auth) NO_AUTH=1; shift ;;
    --help|-h) usage ;;
    --) END_OPTS=1; shift ;;
    # Body markers stay positional; grab the value too so it is never re-parsed.
    --data|--data-binary) [[ $# -ge 2 ]] || usage; POSITIONALS+=("$1" "$2"); shift 2 ;;
    --*) echo "unknown option: $1" >&2; usage ;;
    *) POSITIONALS+=("$1"); shift ;;
  esac
done

set -- "${POSITIONALS[@]:-}"
[[ $# -ge 2 ]] || usage

METHOD="$1"
TARGET="$2"
shift 2

# Resolve TARGET into a full URL. A leading "/" means "a path under $PAAS_API";
# anything with a scheme is used verbatim. PATH_FOR_AUTH is the path we inspect
# to choose a token (empty for full URLs => no auto token).
PATH_FOR_AUTH=""
case "$TARGET" in
  http://*|https://*)
    URL="$TARGET"
    ;;
  /*)
    if [[ -z "${PAAS_API:-}" ]]; then
      echo "PAAS_API is not set; cannot resolve path: $TARGET" >&2
      exit 2
    fi
    URL="${PAAS_API%/}$TARGET"
    PATH_FOR_AUTH="$TARGET"
    ;;
  *)
    echo "URL must be a full http(s):// URL or an absolute path (/...): $TARGET" >&2
    exit 2
    ;;
esac

CURL_ARGS=(-sS --max-time "$TIMEOUT")
HEADERS=()              # human-readable, for dry-run + duplicate detection
HAS_BODY=0

add_header() {
  CURL_ARGS+=(-H "$1")
  HEADERS+=("$1")
}

# Non-GET methods may take a JSON body / @file as the first positional arg.
if [[ "$METHOD" != "GET" && $# -gt 0 ]]; then
  case "$1" in
    --data)        [[ $# -ge 2 ]] || usage; CURL_ARGS+=(-d "$2"); HAS_BODY=1; shift 2 ;;
    --data-binary) [[ $# -ge 2 ]] || usage; CURL_ARGS+=(--data-binary "$2"); HAS_BODY=1; shift 2 ;;
    \{*|\[*)
      CURL_ARGS+=(-d "$1"); HAS_BODY=1
      add_header "Content-Type: application/json"
      shift ;;
    @*) CURL_ARGS+=(--data-binary "$1"); HAS_BODY=1; shift ;;
  esac
fi

# Remaining args are explicit headers.
EXPLICIT_AUTH=0
EXPLICIT_LANE=0
while [[ $# -gt 0 ]]; do
  shopt -s nocasematch
  [[ "$1" == X-API-Key:* ]] && EXPLICIT_AUTH=1
  [[ "$1" == x-lane:*    ]] && EXPLICIT_LANE=1
  shopt -u nocasematch
  add_header "$1"
  shift
done

# Auto-inject the token for path-form targets, unless suppressed or already set.
# One secret covers everything: the dashboard accepts PAAS_TOKEN too, so both
# the /dashboard and /api/paas surfaces use it.
if [[ "$NO_AUTH" -eq 0 && "$EXPLICIT_AUTH" -eq 0 && -n "$PATH_FOR_AUTH" ]]; then
  case "$PATH_FOR_AUTH" in
    /dashboard/*|/api/paas/*)
      [[ -n "${PAAS_TOKEN:-}" ]] && add_header "X-API-Key: $PAAS_TOKEN" ;;
  esac
fi

# Lane header.
if [[ -n "$LANE" && "$EXPLICIT_LANE" -eq 0 ]]; then
  add_header "x-lane: $LANE"
fi

if [[ "${HTTP_DRY_RUN:-0}" == "1" ]]; then
  echo "method=$METHOD"
  echo "url=$URL"
  echo "hasbody=$HAS_BODY"
  for h in "${HEADERS[@]:-}"; do [[ -n "$h" ]] && echo "header=$h"; done
  exit 0
fi

if [[ "$RAW" == "1" ]]; then
  exec curl "${CURL_ARGS[@]}" -X "$METHOD" "$URL"
fi

TMP_BODY="$(mktemp /tmp/http_body.XXXXXX)"
TMP_ERR="$(mktemp /tmp/http_err.XXXXXX)"
trap 'rm -f "$TMP_BODY" "$TMP_ERR"' EXIT

# Capture status + total time in one shot.
METRICS=$(curl "${CURL_ARGS[@]}" -X "$METHOD" -o "$TMP_BODY" \
  -w "%{http_code} %{time_total}" "$URL" 2>"$TMP_ERR")
CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
  CURL_ERR=$(cat "$TMP_ERR" 2>/dev/null || echo "curl failed")
  python3 - "$CURL_EXIT" "$CURL_ERR" <<'PY'
import json, sys
print(json.dumps({"status": 0, "error": f"curl exit {sys.argv[1]}: {sys.argv[2]}"}, ensure_ascii=False))
PY
  exit 1
fi

HTTP_CODE="${METRICS%% *}"
TIME_TOTAL="${METRICS##* }"

[[ -n "$SAVE" ]] && cp "$TMP_BODY" "$SAVE"

# --jq: print the filtered body and exit on its own success.
if [[ -n "$JQ_FILTER" ]]; then
  if ! command -v jq >/dev/null 2>&1; then
    echo '{"status":0,"error":"jq not installed; drop --jq or install jq"}' >&2
    exit 3
  fi
  jq -r "$JQ_FILTER" "$TMP_BODY"
  JQ_EXIT=$?
  if [[ -n "$EXPECT" && "$HTTP_CODE" != "$EXPECT" ]]; then
    echo "expected status $EXPECT, got $HTTP_CODE" >&2
    exit 1
  fi
  exit $JQ_EXIT
fi

python3 - "$HTTP_CODE" "$TIME_TOTAL" "$TMP_BODY" <<'PY' || echo "{\"status\":$HTTP_CODE,\"body\":\"(parse error)\"}"
import json, sys
status = int(sys.argv[1]) if sys.argv[1].isdigit() else 0
ms = round(float(sys.argv[2]) * 1000) if sys.argv[2] else 0
with open(sys.argv[3], "r", encoding="utf-8", errors="replace") as f:
    raw = f.read()
try:
    body = json.loads(raw)
except Exception:
    body = raw
print(json.dumps({"status": status, "ms": ms, "body": body}, ensure_ascii=False))
PY

if [[ -n "$EXPECT" && "$HTTP_CODE" != "$EXPECT" ]]; then
  echo "expected status $EXPECT, got $HTTP_CODE" >&2
  exit 1
fi
exit 0
