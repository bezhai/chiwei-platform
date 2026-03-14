#!/bin/bash
# 标准化 HTTP 请求工具 — 替代所有手写 curl
# 用法:
#   http.sh GET  <url> [header:value ...]
#   http.sh POST <url> '<json_body>' [header:value ...]
#
# 输出: JSON 格式 {"status":<code>,"body":<response>}
# 始终返回 exit 0，状态码在 JSON 中

set -uo pipefail

METHOD="${1:?用法: http.sh METHOD URL [BODY] [HEADERS...]}"
URL="${2:?用法: http.sh METHOD URL [BODY] [HEADERS...]}"
shift 2

BODY=""
CURL_ARGS=(-s --max-time 15)

# POST/PUT/DELETE: 第一个非 header 参数是 body
if [[ "$METHOD" != "GET" ]] && [[ $# -gt 0 ]] && [[ "$1" != *":"* ]]; then
  BODY="$1"
  shift
fi

# 添加 headers
while [[ $# -gt 0 ]]; do
  CURL_ARGS+=(-H "$1")
  shift
done

if [[ -n "$BODY" ]]; then
  CURL_ARGS+=(-H "Content-Type: application/json" -d "$BODY")
fi

# 执行请求，分离 status code 和 body
HTTP_CODE=$(curl "${CURL_ARGS[@]}" -X "$METHOD" -o /tmp/_http_body.txt -w "%{http_code}" "$URL" 2>/tmp/_http_err.txt)
CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
  CURL_ERR=$(cat /tmp/_http_err.txt 2>/dev/null || echo "curl failed")
  echo "{\"status\":0,\"error\":\"curl exit $CURL_EXIT: $CURL_ERR\"}"
  exit 0
fi

RESPONSE_BODY=$(cat /tmp/_http_body.txt 2>/dev/null || echo "")

# 尝试输出为合法 JSON
if python3 -c "import json; json.loads('''$HTTP_CODE''')" 2>/dev/null; then
  : # status is a number, fine
fi

# 用 python 安全地组装 JSON 输出
python3 -c "
import json, sys
status = int('$HTTP_CODE') if '$HTTP_CODE'.isdigit() else 0
body_raw = open('/tmp/_http_body.txt', 'r').read()
try:
    body = json.loads(body_raw)
except:
    body = body_raw
print(json.dumps({'status': status, 'body': body}, ensure_ascii=False))
" 2>/dev/null || echo "{\"status\":$HTTP_CODE,\"body\":\"(parse error)\"}"
