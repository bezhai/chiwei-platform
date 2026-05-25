---
name: api-test
description: 常规 HTTP API 调用 helper。优先用 scripts/http.sh 获得统一 JSON 输出；当需要复杂 curl 能力（长超时、stream、文件、特殊参数、详细调试）时可以直接 curl，并说明原因。
user_invocable: true
---

# /api-test

常规 JSON API 调用优先用 `scripts/http.sh`，它会把响应包装成 `{"status":...,"body":...}`，方便后续判断。

这个 helper 不替代 curl。遇到它不支持的场景，直接用 curl，不要让用户手动代劳。典型场景：stream、文件上传/下载、长时间日志、复杂 TLS/proxy/redirect、需要 `-v` 看握手细节。

## 用法

```bash
# GET
.claude/skills/api-test/scripts/http.sh GET "<url>" [header:value ...]

# POST（第三个参数是 JSON body）
.claude/skills/api-test/scripts/http.sh POST "<url>" '<json>' [header:value ...]

# PUT / DELETE 同理；默认超时 60s
.claude/skills/api-test/scripts/http.sh DELETE "<url>" [header:value ...]

# 指定超时
.claude/skills/api-test/scripts/http.sh --timeout 180 GET "<url>" [header:value ...]

# body 来自文件
.claude/skills/api-test/scripts/http.sh PUT "<url>" @/tmp/body.json "Content-Type: application/json"

# 非 JSON body
.claude/skills/api-test/scripts/http.sh POST "<url>" --data "plain text" "Content-Type: text/plain"

# 需要原生 curl 能力时，显式透传
.claude/skills/api-test/scripts/http.sh --curl -v --max-time 300 -H "X-API-Key: $TOKEN" "<url>"
```

## 输出

始终返回合法 JSON：
```json
{"status": 200, "body": {"key": "value"}}
{"status": 500, "body": {"message": "error detail"}}
{"status": 0, "error": "curl exit 28: timeout"}
```

`--raw` 可直接输出原始 body；`--curl` 会把后面的参数原样交给 curl。

## 示例

```bash
# 健康检查
.claude/skills/api-test/scripts/http.sh GET "$PAAS_API/dashboard/api/health"

# 带认证 + 泳道
.claude/skills/api-test/scripts/http.sh GET "$PAAS_API/dashboard/api/ops/services" \
  "X-API-Key: $DASHBOARD_CC_TOKEN" "x-lane: feat-xxx"

# POST 带 body
.claude/skills/api-test/scripts/http.sh POST "$PAAS_API/dashboard/api/ops/db-query" \
  '{"sql":"SELECT 1","db":"paas_engine"}' \
  "X-API-Key: $DASHBOARD_CC_TOKEN"
```
