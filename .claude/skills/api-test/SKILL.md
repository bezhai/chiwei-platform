---
name: api-test
description: 项目 HTTP API 调用 helper。写路径就行，scripts/http.sh 自动补 $PAAS_API、注 PAAS_TOKEN、透泳道，统一返回 {status, ms, body}。需要复杂 curl 能力（stream、文件、TLS/proxy、详细调试）时直接 curl，并说明原因。
user_invocable: true
---

# /api-test

`scripts/http.sh` 比裸 curl 强的地方：**你只写路径，剩下的它办**。

- 以 `/` 开头的路径自动拼上 `$PAAS_API`；完整 `http(s)://` URL 原样使用。
- 路径在 `/dashboard/...` 或 `/api/paas/...` 下时，自动注入 `X-API-Key: $PAAS_TOKEN`。**对内对外一把钥匙**——dashboard 也认 PAAS_TOKEN，两个面同一个 token，调用方不用再想该传哪个。
- `--lane LANE` 自动加 `x-lane` 头；显式传了 `X-API-Key` / `x-lane` 或 `--no-auth` 时不覆盖。
- 始终返回 `{"status":..,"ms":..,"body":..}`，`ms` 是耗时。

完整 `http(s)://` URL 永远不自动注 token，所以历史上"传完整 URL + 显式 header"的调用方不受影响。

这个 helper 不替代 curl。它不支持的场景直接用 curl，不要让用户手动代劳：stream、文件上传/下载、长日志、复杂 TLS/proxy/redirect、`-v` 看握手。

## 用法

```bash
HTTP=.claude/skills/api-test/scripts/http.sh

# 裸路径：自动补 base + 自动注 PAAS_TOKEN
$HTTP GET /dashboard/api/ops/services
$HTTP GET "/api/paas/apps/agent-service/resolved-config?lane=prod"

# 带泳道
$HTTP --lane ppe-foo GET /api/paas/apps/agent-service/pods

# POST（第三个参数是 JSON body）
$HTTP POST /api/paas/apps/x '{"a":1}'

# 选项放任意位置都认（含末尾）
$HTTP GET /dashboard/api/ops/services --jq '.apps | length'

# 断言状态码：不匹配则退出码非 0（脚本里好用）
$HTTP --expect 200 GET /dashboard/api/health

# 其它选项：--timeout SEC（默认 60）、--save FILE、--no-auth、--raw
$HTTP --timeout 180 GET /dashboard/api/...
$HTTP PUT /api/paas/apps/x @/tmp/body.json
$HTTP POST /api/paas/... --data "plain text" "Content-Type: text/plain"

# 需要原生 curl 能力时显式透传
$HTTP --curl -v --max-time 300 -H "X-API-Key: $PAAS_TOKEN" "<url>"
```

## 选项

- `--lane LANE` 加 `x-lane`（也可用 `HTTP_LANE` 环境变量）
- `--jq FILTER` 对**原始 body**（不是 `{status,ms,body}` 外层）跑 `jq -r`，只打印结果
- `--expect CODE` 状态码 ≠ CODE 时退出码非 0
- `--save FILE` 额外把原始 body 落盘
- `--no-auth` 不自动注 token；`--raw` 直出原始 body（不包 JSON、不计时）
- `--curl <args...>` 后续参数原样交给 curl

## 输出

```json
{"status": 200, "ms": 36, "body": {"key": "value"}}
{"status": 500, "ms": 12, "body": {"message": "error detail"}}
{"status": 0, "error": "curl exit 28: timeout"}
```

## 测试

`scripts/test_http.sh`：dry-run 单测（解析/补 base/注 token/透泳道）+ 本地 server e2e（计时/--jq/--expect）。改 http.sh 后跑 `bash .claude/skills/api-test/scripts/test_http.sh`。
