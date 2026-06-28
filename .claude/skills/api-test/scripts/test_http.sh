#!/usr/bin/env bash
# Tests for http.sh.
#
# Two layers:
#   1. Resolution unit tests via HTTP_DRY_RUN=1 — pin how a call resolves into
#      final URL / auth token / lane header WITHOUT hitting the network. This is
#      the load-bearing behavior: the caller writes a path, the skill picks the
#      right token + base URL so token choice never reaches the caller.
#   2. End-to-end tests against a throwaway local server — verify timing, --jq,
#      and --expect against a real response.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HTTP="$DIR/http.sh"

PASS=0
FAIL=0
ok()  { echo "ok   - $1"; PASS=$((PASS + 1)); }
ko()  { echo "FAIL - $1"; FAIL=$((FAIL + 1)); }

has()    { printf '%s' "$1" | grep -qF -- "$2"; }
assert_has() {     # haystack needle desc
  if has "$1" "$2"; then ok "$3"; else ko "$3 -- missing [$2] in:"; printf '%s\n' "$1" | sed 's/^/      /'; fi
}
assert_missing() { # haystack needle desc
  if has "$1" "$2"; then ko "$3 -- unexpected [$2] in:"; printf '%s\n' "$1" | sed 's/^/      /'; else ok "$3"; fi
}

# ---- Layer 1: resolution (dry run) ----------------------------------------
# One secret for everything: PAAS_TOKEN is injected for both the /api/paas and
# /dashboard surfaces (the dashboard accepts it too). DASHBOARD_CC_TOKEN is set
# here only to prove it is no longer used.
dry() {
  HTTP_DRY_RUN=1 PAAS_API="http://paas.test" \
    DASHBOARD_CC_TOKEN="cctok" PAAS_TOKEN="ptok" \
    "$HTTP" "$@" 2>&1
}

out=$(dry GET /api/paas/apps/x)
assert_has "$out" "url=http://paas.test/api/paas/apps/x" "bare /api/paas path gets PAAS_API prefix"
assert_has "$out" "header=X-API-Key: ptok"               "/api/paas path auto-injects PAAS_TOKEN"

out=$(dry GET /dashboard/api/ops/services)
assert_has     "$out" "url=http://paas.test/dashboard/api/ops/services" "bare /dashboard path gets PAAS_API prefix"
assert_has     "$out" "header=X-API-Key: ptok"  "/dashboard path uses the one PAAS_TOKEN too"
assert_missing "$out" "header=X-API-Key: cctok" "/dashboard path no longer uses DASHBOARD_CC_TOKEN"

out=$(dry GET "https://full.example/api/paas/apps/x")
assert_has     "$out" "url=https://full.example/api/paas/apps/x" "full URL kept verbatim (no prefix)"
assert_missing "$out" "X-API-Key"                               "full URL never auto-injects a token (back-compat)"

out=$(dry --lane ppe-foo GET /api/paas/apps/x)
assert_has "$out" "header=x-lane: ppe-foo" "--lane adds x-lane header"
assert_has "$out" "header=X-API-Key: ptok" "--lane still auto-injects token"

out=$(dry GET /api/paas/apps/x "X-API-Key: override")
assert_has     "$out" "header=X-API-Key: override" "explicit X-API-Key is honored"
assert_missing "$out" "X-API-Key: ptok"            "explicit X-API-Key suppresses auto token"

out=$(dry --no-auth GET /api/paas/apps/x)
assert_missing "$out" "X-API-Key" "--no-auth skips token injection"

out=$(dry POST /api/paas/apps/x '{"a":1}')
assert_has "$out" "method=POST"                         "POST method preserved"
assert_has "$out" "hasbody=1"                           "JSON body detected on POST"
assert_has "$out" "header=Content-Type: application/json" "JSON body sets Content-Type"
assert_has "$out" "header=X-API-Key: ptok"             "POST to /api/paas auto-injects token"

# Options must be honored wherever they appear, not only before METHOD — the
# natural habit is to tack --jq / --lane onto the end of a call.
out=$(dry GET /api/paas/apps/x --lane ppe-bar)
assert_has     "$out" "header=x-lane: ppe-bar" "trailing --lane is honored"
assert_missing "$out" "header=--lane"          "trailing --lane is not sent as a header"

out=$(dry GET /api/paas/apps/x --no-auth)
assert_missing "$out" "X-API-Key" "trailing --no-auth is honored"

# ---- Layer 2: end-to-end against a local server ---------------------------
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
SRV=$(mktemp /tmp/http_srv.XXXXXX.py)
cat >"$SRV" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _h(self):
        if self.path == "/boom":
            self.send_response(500); self.end_headers(); self.wfile.write(b'{"message":"boom"}')
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"hello":"world","n":42}')
    def do_GET(self): self._h()
    def do_POST(self): self._h()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
python3 "$SRV" "$PORT" &
SRV_PID=$!
trap 'kill "$SRV_PID" 2>/dev/null; rm -f "$SRV"' EXIT
for _ in $(seq 1 50); do
  curl -sS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 0.1
done
BASE="http://127.0.0.1:$PORT"

out=$("$HTTP" GET "$BASE/ok")
assert_has "$out" '"status": 200'      "e2e: status surfaced"
assert_has "$out" '"hello": "world"'   "e2e: body parsed as JSON"
assert_has "$out" '"ms":'              "e2e: timing (ms) reported"

out=$("$HTTP" --jq '.n' GET "$BASE/ok")
assert_has "$out" "42" "--jq extracts a field"

out=$("$HTTP" GET "$BASE/ok" --jq '.n')
assert_has "$out" "42" "trailing --jq extracts a field"

"$HTTP" --expect 200 GET "$BASE/ok" >/dev/null 2>&1
if [[ $? -eq 0 ]]; then ok "--expect 200 passes on a 200"; else ko "--expect 200 should pass on a 200"; fi

"$HTTP" --expect 200 GET "$BASE/boom" >/dev/null 2>&1
if [[ $? -ne 0 ]]; then ok "--expect 200 fails (nonzero exit) on a 500"; else ko "--expect 200 should fail on a 500"; fi

echo "-----"
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
