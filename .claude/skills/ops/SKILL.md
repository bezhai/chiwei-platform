---
name: ops
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

### `gateway` — api-gateway 路由规则调度

调度 api-gateway 的流量路由规则：预览命中、止血启停、调整权重。所有动作经 Dashboard 中转写审计，再转发到 paas-engine 的 gateway-rules 管理 API。

> ⚠️ **依赖 Dashboard 中转端点（当前为 gap，见文末「Dashboard 待补能力」）。** 在 Dashboard 落地 `/ops/gateway-rules*` 中转 + 审计前，下列命令会 404，不可用于线上调度。

#### `gateway explain PATH [LANE]` — 预览一个请求会命中哪条规则

```bash
# LANE 可空（代表请求不带 x-lane）
$HTTP POST "$BASE/ops/gateway-rules:explain" \
  '{"path":"<PATH>","x_lane":"<LANE>"}' \
  "$AUTH"
```

返回：是否命中、命中规则名 + 原因、would_forward / would_redirect、候选 targets（含 effective_lane）、其余规则未命中原因（disabled / request_lane 不匹配 / path 不匹配 / 被更高优先级 shadowed）。**上线权重分流前必须先 explain 确认命中符合预期。**

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

### `skills` — 列出所有 Agent 技能

```bash
$HTTP GET "$BASE/skills" "$AUTH"
```

### `skill NAME` — 查看技能详情（文件列表 + SKILL.md 内容）

```bash
$HTTP GET "$BASE/skills/<NAME>" "$AUTH"
```

### `skill-create NAME` — 创建新技能

从本地目录读取文件并上传。目录结构：

```
NAME/
  SKILL.md         # 必须
  scripts/         # 可选
    run.py
```

步骤：
1. 读取当前工作目录下的 `NAME/SKILL.md`，用内容调用 POST 创建
2. 如果有 `NAME/scripts/` 目录，逐个读取文件，调用 PUT 上传

```bash
# 创建 skill（读取本地 SKILL.md）
CONTENT=$(cat "<NAME>/SKILL.md")
$HTTP POST "$BASE/skills" "{\"name\":\"<NAME>\",\"content\":$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$CONTENT")}" "$AUTH"

# 上传脚本（如果有 scripts/ 目录）
for f in <NAME>/scripts/*; do
  FNAME=$(basename "$f")
  FCONTENT=$(cat "$f")
  $HTTP PUT "$BASE/skills/<NAME>/files/scripts/$FNAME" "{\"content\":$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$FCONTENT")}" "$AUTH"
done
```

### `skill-edit NAME FILE` — 编辑技能文件

获取文件内容、编辑后上传：

```bash
# 读取
$HTTP GET "$BASE/skills/<NAME>/files/<FILE>" "$AUTH"

# 写入（修改后）
$HTTP PUT "$BASE/skills/<NAME>/files/<FILE>" '{"content":"<新内容>"}' "$AUTH"
```

### `skill-delete NAME` — 删除技能

```bash
$HTTP DELETE "$BASE/skills/<NAME>" "$AUTH"
```

## 注意事项

- 写操作（bind/unbind/gateway disable/enable/set-weights/skill-create/skill-edit/skill-delete）影响线上，执行前先告知用户
- 不涵盖 deploy/undeploy/release/self-deploy/logs，这些仍走 `make` 命令

## Dashboard 待补能力（gateway 调度的跨 repo gap）

`gateway` 子命令依赖 Dashboard（不在本 repo）新增以下中转端点，本 repo 只完成了 paas-engine 引擎侧 API（`/api/paas/gateway-rules:explain`、`/{name}:disable`、`:enable`、`:set-weights`）。**在 Dashboard 落地前 gateway 命令不可用。**

Dashboard 需新增并把请求转发到 paas-engine 对应端点：

| Dashboard 中转端点 | 转发到 paas-engine | 审计 |
|---|---|---|
| `POST /dashboard/api/ops/gateway-rules:explain` | `POST /api/paas/gateway-rules:explain` | 只读，可不审计 |
| `POST /dashboard/api/ops/gateway-rules/{name}:disable` | 同名 | 必须审计 |
| `POST /dashboard/api/ops/gateway-rules/{name}:enable` | 同名 | 必须审计 |
| `POST /dashboard/api/ops/gateway-rules/{name}:set-weights` | 同名 | 必须审计 |

止血动作（disable/enable/set-weights）的审计必须记录：操作者、规则名、before→after 值、reason、生效时间、当前快照版本。paas-engine 端点已在响应里返回 before/after 供 Dashboard 落审计。**在审计中转就绪前，止血端点不应投入 ops 日常使用**（否则出现「能改但未被审计」的空窗）。
