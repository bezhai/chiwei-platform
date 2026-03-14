---
description: 统一运维查询和操作。替代 make status/pods/lane-bind 等命令，所有操作自动审计。
user_invocable: true
---

# /ops

通过 Dashboard API 执行运维操作，所有调用自动记录审计日志。

## 参数

```
!`echo "$ARGUMENTS"`
```

## 公共配置

所有请求通过 `http.sh` 发送，公共参数：

- **Base URL**: `$PAAS_API/dashboard/api`
- **认证 Header**: `X-API-Key: $DASHBOARD_CC_TOKEN`

```bash
HTTP=".claude/skills/api-test/scripts/http.sh"
BASE="$PAAS_API/dashboard/api"
AUTH="X-API-Key: $DASHBOARD_CC_TOKEN"
```

## 子命令

### `status [APP]` — 服务状态

```bash
$HTTP GET "$BASE/ops/services" "$AUTH"
```

返回全部服务及 Release 状态。如果指定了 APP，从返回结果中过滤展示。

### `pods APP [LANE]` — Pod 状态

```bash
# LANE 默认 prod
$HTTP GET "$BASE/ops/services/<APP>/pods?lane=<LANE>" "$AUTH"
```

### `latest-build APP` — 最近成功构建

```bash
$HTTP GET "$BASE/ops/builds/<APP>/latest" "$AUTH"
```

### `bindings` — 泳道绑定列表

```bash
$HTTP GET "$BASE/ops/lane-bindings" "$AUTH"
```

### `bind TYPE KEY LANE` — 创建泳道绑定

```bash
$HTTP POST "$BASE/ops/lane-bindings" \
  '{"route_type":"<TYPE>","route_key":"<KEY>","lane_name":"<LANE>"}' \
  "$AUTH"
```

### `unbind TYPE KEY` — 删除泳道绑定

```bash
$HTTP DELETE "$BASE/ops/lane-bindings?type=<TYPE>&key=<KEY>" "$AUTH"
```

### `audit [caller=xxx] [action=xxx]` — 审计日志查询

```bash
# 拼接 query params: caller, action, page, limit
$HTTP GET "$BASE/audit-logs?caller=<caller>&action=<action>" "$AUTH"
```

### `activity [days=7]` — 赤尾活动概览

```bash
$HTTP GET "$BASE/activity/overview?days=<days>" "$AUTH"
```

### `diary-status` — 日记/周记生成状态

```bash
$HTTP GET "$BASE/activity/diary-status" "$AUTH"
```

## 注意事项

- 写操作（bind/unbind）影响线上，执行前先告知用户
- 不涵盖 deploy/undeploy/release/self-deploy/logs，这些仍走 `make` 命令
