#!/bin/sh
set -eu

# --- Config ---
PROMETHEUS="${PROMETHEUS_URL:-http://kube-prometheus-stack-prometheus.monitoring:9090}"
FEISHU_URL="${FEISHU_WEBHOOK_URL:?FEISHU_WEBHOOK_URL is required}"
CST_OFFSET=8

# --- Helpers ---
now_cst() {
  date -u -d "+${CST_OFFSET} hours" '+%m-%d %H:%M:%S' 2>/dev/null \
    || date -u '+%m-%d %H:%M:%S'
}

prom_query() {
  curl -sf --max-time 10 "${PROMETHEUS}/api/v1/query" --data-urlencode "query=$1" \
    | jq -r '.data.result[0].value[1] // empty'
}

prom_query_all() {
  curl -sf --max-time 10 "${PROMETHEUS}/api/v1/query" --data-urlencode "query=$1" \
    | jq -c '.data.result // []'
}

# --- 1. Service Health Checks ---
SERVICES="api-gateway|http://api-gateway.prod:8080/healthz
paas-engine|http://paas-engine.prod:8080/healthz
lite-registry|http://lite-registry.prod:8080/healthz
lark-server|http://lark-server.prod:3000/api/health
lark-proxy|http://lark-proxy.prod:3003/api/health
agent-service|http://agent-service.prod:8000/health
tool-service|http://tool-service.prod:8000/health"

health_lines=""
healthy=0
total=0

echo "$SERVICES" | while IFS='|' read -r name url; do
  total=$((total + 1))
  if curl -sf --max-time 5 "$url" >/dev/null 2>&1; then
    healthy=$((healthy + 1))
    health_lines="${health_lines}✅ ${name}  "
  else
    health_lines="${health_lines}❌ ${name}  "
  fi
  # Write to temp file since subshell
  echo "${healthy}|${total}|${health_lines}" > /tmp/health_result
done

# Read results from subshell
IFS='|' read -r healthy total health_lines < /tmp/health_result

# --- 2. Node Resources (avg across nodes) ---
cpu_pct=$(prom_query '100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100' | xargs printf '%.0f' 2>/dev/null || echo "N/A")
mem_pct=$(prom_query '(1 - avg(node_memory_AvailableBytes / node_memory_MemTotalBytes)) * 100' | xargs printf '%.0f' 2>/dev/null || echo "N/A")
disk_pct=$(prom_query 'max(100 - (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100)' | xargs printf '%.0f' 2>/dev/null || echo "N/A")

# --- 3. Pod Status ---
not_running=$(prom_query 'count(kube_pod_status_phase{namespace="prod",phase!="Running",phase!="Succeeded"}) or vector(0)' | xargs printf '%.0f' 2>/dev/null || echo "0")

# --- 4. Alerts in last 24h ---
alerts_24h=$(prom_query 'count(ALERTS{alertstate="firing"}) or vector(0)' | xargs printf '%.0f' 2>/dev/null || echo "0")

# --- Determine status ---
has_issue="false"
if [ "$healthy" -lt "$total" ] || [ "$not_running" != "0" ] || [ "$alerts_24h" != "0" ]; then
  has_issue="true"
fi

if [ "$has_issue" = "true" ]; then
  color="red"
  title="📋 每日系统巡检报告（有异常）"
else
  color="blue"
  title="📋 每日系统巡检报告"
fi

report_time=$(now_cst)

# --- Build Feishu Card ---
card_json=$(cat <<ENDJSON
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {"tag": "plain_text", "content": "${title}"},
      "template": "${color}"
    },
    "elements": [
      {
        "tag": "div",
        "fields": [
          {
            "is_short": false,
            "text": {"tag": "lark_md", "content": "**🟢 服务状态 (${healthy}/${total} 正常)**\n${health_lines}"}
          }
        ]
      },
      {
        "tag": "div",
        "fields": [
          {
            "is_short": true,
            "text": {"tag": "lark_md", "content": "**📊 节点资源**\nCPU: ${cpu_pct}%  内存: ${mem_pct}%  磁盘: ${disk_pct}%"}
          }
        ]
      },
      {
        "tag": "div",
        "fields": [
          {
            "is_short": true,
            "text": {"tag": "lark_md", "content": "**🔔 当前活跃告警:** ${alerts_24h} 条"}
          },
          {
            "is_short": true,
            "text": {"tag": "lark_md", "content": "**⚠️ 异常 Pod:** ${not_running} 个"}
          }
        ]
      },
      {
        "tag": "div",
        "fields": [
          {
            "is_short": false,
            "text": {"tag": "lark_md", "content": "**⏰ 报告时间:** ${report_time}"}
          }
        ]
      }
    ]
  }
}
ENDJSON
)

# --- Send ---
resp=$(curl -sf --max-time 10 -X POST "$FEISHU_URL" \
  -H 'Content-Type: application/json' \
  -d "$card_json" 2>&1) || true

echo "Report sent at ${report_time}"
echo "Response: ${resp}"
