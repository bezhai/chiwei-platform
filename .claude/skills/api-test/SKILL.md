---
name: api-test
description: 标准化 HTTP API 调用工具。禁止直接写 curl 命令，所有 HTTP 请求必须通过此 skill 的 scripts/http.sh 脚本执行。当需要调用、测试、调试、验证任何 HTTP API 端点时使用。
---

# /api-test

**禁止直接写 curl。所有 HTTP 请求通过 `scripts/http.sh` 执行。**

## 用法

```bash
# GET
.claude/skills/api-test/scripts/http.sh GET "<url>" [header:value ...]

# POST（第三个参数是 JSON body）
.claude/skills/api-test/scripts/http.sh POST "<url>" '<json>' [header:value ...]

# PUT / DELETE 同理
.claude/skills/api-test/scripts/http.sh DELETE "<url>" [header:value ...]
```

## 输出

始终返回合法 JSON：
```json
{"status": 200, "body": {"key": "value"}}
{"status": 500, "body": {"message": "error detail"}}
{"status": 0, "error": "curl exit 28: timeout"}
```

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
