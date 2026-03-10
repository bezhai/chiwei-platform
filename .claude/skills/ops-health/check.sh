#!/usr/bin/env bash
# Health check all services via kubectl exec into a cluster pod.
# Output: one line per service: "<name> <port> <status>"
# status: 200 = OK, 000 = TIMEOUT/unreachable

SERVICES="agent-service:8000 lark-server:3000 lark-proxy:3003 api-gateway:8080 tool-service:8000 paas-engine:8080 lite-registry:8080 alert-webhook:8080 monitor-dashboard:3002"

kubectl exec deploy/api-gateway-prod -n prod -- sh -c "
for svc in $SERVICES; do
  name=\${svc%%:*}
  port=\${svc##*:}
  if wget -q -O /dev/null --timeout=2 http://\${name}:\${port}/healthz 2>/dev/null; then
    echo \"\${name} \${port} 200\"
  else
    echo \"\${name} \${port} 000\"
  fi
done
" 2>&1
