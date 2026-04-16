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

- 写操作（bind/unbind/skill-create/skill-edit/skill-delete）影响线上，执行前先告知用户
- 不涵盖 deploy/undeploy/release/self-deploy/logs，这些仍走 `make` 命令
