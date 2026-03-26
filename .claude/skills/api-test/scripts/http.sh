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

# POST/PUT/DELETE: 以 { 或 [ 开头的参数是 JSON body
if [[ "$METHOD" != "GET" ]] && [[ $# -gt 0 ]] && [[ "$1" == "{"* || "$1" == "["* ]]; then
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

# 用进程 ID 隔离临时文件，避免并发竞态
_TMP_BODY="/tmp/_http_body_$$.txt"
_TMP_ERR="/tmp/_http_err_$$.txt"
trap 'rm -f "$_TMP_BODY" "$_TMP_ERR"' EXIT

# 执行请求，分离 status code 和 body
HTTP_CODE=$(curl "${CURL_ARGS[@]}" -X "$METHOD" -o "$_TMP_BODY" -w "%{http_code}" "$URL" 2>"$_TMP_ERR")
CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
  CURL_ERR=$(cat "$_TMP_ERR" 2>/dev/null || echo "curl failed")
  echo "{\"status\":0,\"error\":\"curl exit $CURL_EXIT: $CURL_ERR\"}"
  exit 0
fi

# 用 python 安全地组装 JSON 输出
python3 -c "
import json, sys
status = int('$HTTP_CODE') if '$HTTP_CODE'.isdigit() else 0
body_raw = open('$_TMP_BODY', 'r').read()
try:
    body = json.loads(body_raw)
except:
    body = body_raw
print(json.dumps({'status': status, 'body': body}, ensure_ascii=False))
" 2>/dev/null || echo "{\"status\":$HTTP_CODE,\"body\":\"(parse error)\"}"
