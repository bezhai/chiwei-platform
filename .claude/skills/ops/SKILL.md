---
name: ops
description: 统一运维查询和受控操作入口。用于服务状态、Pod、构建、泳道绑定、gateway rules、审计日志；不承接部署、日志、数据库、Langfuse、skill 管理等非运维核心动作。
user_invocable: true
---

# /ops

通过 Dashboard API 执行运维查询和受控操作，所有调用自动记录审计日志。

边界：
- 负责：服务状态、Pod、最近构建、泳道绑定、gateway rules、审计日志。
- 不负责：部署/下线/发布（走 `make` 或 `/ship`）、应用日志（走 `make logs`）、数据库访问（走 `/ops-db`）、Langfuse（走 langfuse skill）、skill 文件管理。

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

### `gateway` — api-gateway 路由规则调度

调度 api-gateway 的流量路由规则：查看配置、预览命中、增删改、止血启停、调权、回滚。所有写动作经 Dashboard 中转写审计（caller/规则名/before→after/reason/快照版本），再转发到 paas-engine 的 gateway-rules 管理 API。

#### `gateway list` — 列出全部规则（只读）

```bash
$HTTP GET "$BASE/ops/gateway-rules" "$AUTH"
```

#### `gateway get NAME` — 看单条规则（只读）

```bash
$HTTP GET "$BASE/ops/gateway-rules/<NAME>" "$AUTH"
```

#### `gateway snapshot` — 看 paas-engine 当前下发的期望配置（version + 规则，只读）

```bash
$HTTP GET "$BASE/ops/gateway-rules/snapshot" "$AUTH"
```

这是「流量调度配置长什么样」的权威来源——paas-engine 的当前快照，即 api-gateway 应当执行的规则。看调度现状用这个。

#### `gateway snapshots [LIMIT]` — 列出最近 N 条规则快照历史（只读）

```bash
$HTTP GET "$BASE/ops/gateway-rules/snapshots?limit=<LIMIT>" "$AUTH"
```

每条快照含 snapshot_version / created_by / reason / created_at / 规则全量。用于回滚前定位目标版本。

#### `gateway explain PATH [LANE]` — 预览一个请求会命中哪条规则

```bash
# LANE 可空（代表请求不带 x-lane）
$HTTP POST "$BASE/ops/gateway-rules:explain" \
  '{"path":"<PATH>","x_lane":"<LANE>"}' \
  "$AUTH"
```

返回：是否命中、命中规则名 + 原因、would_forward / would_redirect、候选 targets（含 effective_lane）、是否启用稳定分流（stable_split + split_key_headers）、其余规则未命中原因（disabled / request_lane 不匹配 / path 不匹配 / 被更高优先级 shadowed）。**上线权重分流前必须先 explain 确认命中符合预期。**

#### `gateway disable NAME REASON` — 停用一条规则（止血）

```bash
$HTTP POST "$BASE/ops/gateway-rules/<NAME>:disable" \
  '{"reason":"<REASON>"}' \
  "$AUTH"
```

返回 before/after 的 enabled 值。⚠️ 不要把入口的全部规则都 disable——api-gateway 会拒绝「无任何 enabled 规则」的快照并保留 last-good，导致 disable 不生效。

#### `gateway enable NAME REASON` — 重新启用一条规则

```bash
$HTTP POST "$BASE/ops/gateway-rules/<NAME>:enable" \
  '{"reason":"<REASON>"}' \
  "$AUTH"
```

#### `gateway set-weights NAME REASON` — 整体替换一条规则的 target 权重（止血/灰度）

整体替换该规则**全部** target 的权重，按 `service`+`lane` 标识每个 target。请求体字段名是 `weights`（数组，每项 `{service, lane, weight}`）。权重总和必须 = 100，单个可为 0（把某 target 改 0 即把流量切走），不可缺失或多出 target。

```bash
# 示例：把 agent-canary 规则切回 prod（prod=100, ppe-new=0）
$HTTP POST "$BASE/ops/gateway-rules/<NAME>:set-weights" \
  '{"reason":"<REASON>","weights":[{"service":"agent-service","lane":"prod","weight":100},{"service":"agent-service","lane":"ppe-new","weight":0}]}' \
  "$AUTH"
```

返回 before/after 的 targets 权重。target 集合必须与规则现有 target 完全一致（缺失/多余/重复都会被拒绝）。

#### `gateway upsert NAME REASON` — 创建或更新一条规则（写）

按 NAME 整体创建/覆盖一条规则。请求体字段：`path_prefix`（路径前缀，必须 `/` 开头）、`match.path_prefix`（匹配条件，与顶层 path_prefix 一致）、`targets`（每项含 `service`/`lane`/`port`/`weight`，weight 总和=100，lane 留空表示跟随请求 x-lane 透传）、`priority`、`enabled`、可选 `split_key_headers`。`split_key_headers` 非空时启用稳定分流：按请求里第一个命中的 header 值做 hash 分桶，同一来源稳定落同一 target；为空则退化为加权随机。

```bash
$HTTP PUT "$BASE/ops/gateway-rules/<NAME>" \
  '{"reason":"<REASON>","priority":10,"enabled":true,"path_prefix":"/api/agent/","match":{"path_prefix":"/api/agent/"},"targets":[{"service":"agent-service","lane":"","port":8000,"weight":100}],"split_key_headers":["x-user-id"]}' \
  "$AUTH"
```

#### `gateway delete NAME REASON` — 删除一条规则（写）

```bash
$HTTP DELETE "$BASE/ops/gateway-rules/<NAME>" \
  '{"reason":"<REASON>"}' \
  "$AUTH"
```

#### `gateway rollback VERSION REASON` — 回滚到历史某快照版本（写）

把整套规则回滚到 `gateway snapshots` 列出的某个 `snapshot_version`，会分配一个新的、更高的快照版本（不是原地复活旧版本号）。回滚前先用 `gateway snapshots` 确认目标版本内容。

```bash
$HTTP POST "$BASE/ops/gateway-rules:rollback" \
  '{"reason":"<REASON>","snapshot_version":<VERSION>}' \
  "$AUTH"
```

### `audit [caller=xxx] [action=xxx]` — 审计日志查询

```bash
# 拼接 query params: caller, action, page, limit
$HTTP GET "$BASE/audit-logs?caller=<caller>&action=<action>" "$AUTH"
```

## 注意事项

- 写操作（bind/unbind/gateway upsert/delete/disable/enable/set-weights/rollback）影响线上，执行前先告知用户
- 不涵盖 deploy/undeploy/release/self-deploy/logs，这些仍走 `make` 命令
