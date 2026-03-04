#!/usr/bin/env bash
#
# node-watchdog.sh - 控制节点外部拨测告警
#
# 从外部节点（cpu2）独立监测控制节点（cpu1）健康状态，
# 挂了直接调飞书 webhook 报警，绕过 K8s 内部监控链路。
#
# 部署方式（在 cpu2 上配置 crontab）：
#   * * * * * FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx WATCH_HOST=10.37.6.235 /path/to/node-watchdog.sh >> /tmp/node-watchdog.log 2>&1
#
# 环境变量：
#   FEISHU_WEBHOOK_URL  - 飞书 webhook 地址（必填）
#   WATCH_HOST          - 被监控节点 IP（必填）
#   STATE_FILE          - 状态文件路径（默认 /tmp/node-watchdog.state）
#   K8S_API_PORT        - K8s API Server 端口（默认 6443）
#
set -euo pipefail

# --- 配置 ---
: "${FEISHU_WEBHOOK_URL:?环境变量 FEISHU_WEBHOOK_URL 未设置}"
: "${WATCH_HOST:?环境变量 WATCH_HOST 未设置}"
STATE_FILE="${STATE_FILE:-/tmp/node-watchdog.state}"
K8S_API_PORT="${K8S_API_PORT:-6443}"
TIMEOUT=5

# --- 检测函数 ---
check_ping() {
    ping -c 1 -W "$TIMEOUT" "$WATCH_HOST" >/dev/null 2>&1
}

check_k8s_api() {
    curl -sk --connect-timeout "$TIMEOUT" --max-time "$TIMEOUT" \
        "https://${WATCH_HOST}:${K8S_API_PORT}/healthz" >/dev/null 2>&1
}

# --- 飞书卡片发送 ---
send_feishu_card() {
    local status="$1"  # firing | resolved
    local title="$2"
    local details="$3"
    local template timestamp

    if [ "$status" = "firing" ]; then
        template="red"
    else
        template="green"
    fi
    timestamp=$(date '+%m-%d %H:%M:%S')

    local payload
    payload=$(cat <<ENDJSON
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {
        "tag": "plain_text",
        "content": "${title}"
      },
      "template": "${template}"
    },
    "elements": [
      {
        "tag": "div",
        "fields": [
          {
            "is_short": true,
            "text": {
              "tag": "lark_md",
              "content": "**状态:** $([ "$status" = "firing" ] && echo "告警触发 🔥" || echo "已恢复 ✅")"
            }
          },
          {
            "is_short": true,
            "text": {
              "tag": "lark_md",
              "content": "**节点:** ${WATCH_HOST}"
            }
          }
        ]
      },
      {
        "tag": "div",
        "fields": [
          {
            "is_short": false,
            "text": {
              "tag": "lark_md",
              "content": "**详情:** ${details}"
            }
          }
        ]
      },
      {
        "tag": "div",
        "fields": [
          {
            "is_short": true,
            "text": {
              "tag": "lark_md",
              "content": "**时间:** ${timestamp}"
            }
          },
          {
            "is_short": true,
            "text": {
              "tag": "lark_md",
              "content": "**来源:** 外部拨测 (node-watchdog)"
            }
          }
        ]
      }
    ]
  }
}
ENDJSON
)

    curl -s -X POST "$FEISHU_WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "$payload" >/dev/null 2>&1 || echo "[$(date)] 飞书发送失败"
}

# --- 主逻辑 ---
failures=""

if ! check_ping; then
    failures="${failures}ICMP ping 不通\n"
fi

if ! check_k8s_api; then
    failures="${failures}K8s API Server (${K8S_API_PORT}) 不可达\n"
fi

# 读取上次状态（ok / fail）
prev_state="ok"
[ -f "$STATE_FILE" ] && prev_state=$(cat "$STATE_FILE")

if [ -n "$failures" ]; then
    # 当前异常
    if [ "$prev_state" != "fail" ]; then
        # 状态变更：ok → fail，发告警
        details=$(echo -e "$failures" | sed '/^$/d' | tr '\n' '，' | sed 's/，$//')
        send_feishu_card "firing" "控制节点不可达 🚨" "$details"
        echo "[$(date)] 告警已发送: $details"
    else
        echo "[$(date)] 仍然异常，不重复告警"
    fi
    echo -n "fail" > "$STATE_FILE"
else
    # 当前正常
    if [ "$prev_state" = "fail" ]; then
        # 状态变更：fail → ok，发恢复
        send_feishu_card "resolved" "控制节点已恢复" "所有检测项恢复正常（ICMP ping、K8s API Server）"
        echo "[$(date)] 恢复通知已发送"
    fi
    echo -n "ok" > "$STATE_FILE"
fi
