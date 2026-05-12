# Dev Workflow v2 — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 prod 已有的 `qdrant` 和 `mongo` ConfigBundle 加 `class_overrides[coe]` + `required_keys[coe]`，让 coe-* lane 部署时派 chiwei-test 容器；同步补 Phase 2 漏 commit 的 rabbitmq image 改动。

**Architecture:** 沿用 Phase 1+2 ClassOverrides + RequiredKeys 模板。Phase 3 关键差异：bundle 已存在不新建，5 App 已引用不追加 config_bundles，**纯 bundle 字段扩展 + 新增 cpu1 docker 测试容器 + git 补 commit**。

**Tech Stack:** Docker Compose / PaaS Engine HTTP API (`$PAAS_API`) / make 部署命令 / `/ops` `/ops-db` skill / qdrant-client / mongodb client。

**Spec:** `docs/superpowers/specs/2026-05-12-dev-workflow-v2-phase-3-design.md`

---

## 文件改动总览

**改 1 个文件**：

- `infra/test-env/docker-compose.yaml` — 加 `chiwei-test-qdrant` + `chiwei-test-mongo` services；rabbitmq image 从默认改为 `harbor.local:30002/inner-bot/rabbitmq:<fixed-tag>` 同步进 git；加 2 个新 volumes 声明。

**其余改动不进 git**（写 paas-engine DB + 操作 cpu1 docker + 部署验证）：

- `paas-engine.config_bundles.qdrant` — class_overrides + required_keys 字段 PUT
- `paas-engine.config_bundles.mongo` — class_overrides + required_keys 字段 PUT
- cpu1 docker：`chiwei-test-qdrant` / `chiwei-test-mongo` 容器拉起
- k3s prod ns：coe-validation lane deploy 5 App + 验证 + undeploy

---

## Task 1: 拿到 rabbitmq 当前 image 实际 tag/digest

**目的**：spec §3.4 要求 image 固定 tag/digest，避免 `:latest` 漂移。docker-compose.yaml 写入前必须知道实际 tag。

**Files:** 无文件改动，纯查询

- [ ] **Step 1: ssh cpu1 查 chiwei-test-rabbitmq 容器实际 image**

Run:
```bash
ssh cpu1 'docker inspect chiwei-test-rabbitmq --format "{{.Config.Image}} {{.Image}}"'
```

Expected output 形如：
```
harbor.local:30002/inner-bot/rabbitmq:latest sha256:abc123...
```

记录两个值：`<image-name>` 和 `<image-digest-sha256>`。digest 是稳定的（即使 latest 移动了），后面 docker-compose.yaml 写入用 digest 形式：`harbor.local:30002/inner-bot/rabbitmq@sha256:abc123...`

- [ ] **Step 2: 验证 plugin 已 enabled**

Run:
```bash
ssh cpu1 'docker exec chiwei-test-rabbitmq rabbitmq-plugins list -E rabbitmq_delayed_message_exchange'
```

Expected output 含 `[E*] rabbitmq_delayed_message_exchange` (有 `E*` 标记，enabled)。

如果没 enabled，**立即停止 plan 执行**，跟用户确认下一步（plugin 缺失说明 Phase 2 ship 时的容器跟当前不一致）。

- [ ] **Step 3: 把 digest 记下来**

把 Step 1 拿到的 `<image-digest-sha256>` 直接写在 plan 文档里（这一行下面），后续 Task 2 直接引用：

```
RABBITMQ_IMAGE = harbor.local:30002/inner-bot/rabbitmq@sha256:<填入 step 1 digest>
```

无 commit。

---

## Task 2: 改 docker-compose.yaml 加 qdrant + mongo + fix rabbitmq image

**Files:**
- Modify: `infra/test-env/docker-compose.yaml`

- [ ] **Step 1: Edit `infra/test-env/docker-compose.yaml`，rabbitmq image 改成 fixed digest**

把：
```yaml
  chiwei-test-rabbitmq:
    image: rabbitmq:3.13-management-alpine
```

改为（image 用 Task 1 Step 3 记录的 digest）：
```yaml
  chiwei-test-rabbitmq:
    image: harbor.local:30002/inner-bot/rabbitmq@sha256:<TASK1_STEP3_DIGEST>
```

保留 container_name / restart / environment / ports / volumes / healthcheck 不变。

- [ ] **Step 2: 在 `chiwei-test-redis` service 后追加 `chiwei-test-qdrant`**

在 `chiwei-test-redis` block 后、`volumes:` 顶级声明前，加：

```yaml
  chiwei-test-qdrant:
    image: qdrant/qdrant:v1.11.0
    container_name: chiwei-test-qdrant
    restart: unless-stopped
    environment:
      QDRANT__SERVICE__API_KEY: ${CHIWEI_TEST_QDRANT_API_KEY:?qdrant api key required}
    ports:
      - "16333:6333"  # HTTP REST API (prod qdrant 占 6333，test 用 16333)
      - "16334:6334"  # gRPC
    volumes:
      - chiwei_test_qdrant_data:/qdrant/storage
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- --header=\"api-key: $$QDRANT__SERVICE__API_KEY\" http://localhost:6333/readyz | grep -q ok"]
      interval: 10s
      timeout: 5s
      retries: 5
```

- [ ] **Step 3: 在 `chiwei-test-qdrant` 后追加 `chiwei-test-mongo`**

```yaml
  chiwei-test-mongo:
    image: mongo:7.0.14-jammy
    container_name: chiwei-test-mongo
    restart: unless-stopped
    environment:
      MONGO_INITDB_ROOT_USERNAME: chiwei-test
      MONGO_INITDB_ROOT_PASSWORD: ${CHIWEI_TEST_MONGO_PASSWORD:?mongo password required}
    ports:
      - "27018:27017"  # prod mongo 占 27017，test 用 27018
    volumes:
      - chiwei_test_mongo_data:/data/db
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok", "--username", "chiwei-test", "--password", "$$MONGO_INITDB_ROOT_PASSWORD", "--authenticationDatabase", "admin"]
      interval: 10s
      timeout: 5s
      retries: 5
```

- [ ] **Step 4: 在顶级 `volumes:` block 追加两个新卷**

把：
```yaml
volumes:
  chiwei_test_pg_data:
    name: chiwei_test_pg_data
  chiwei_test_mq_data:
    name: chiwei_test_mq_data
  chiwei_test_redis_data:
    name: chiwei_test_redis_data
```

改为：
```yaml
volumes:
  chiwei_test_pg_data:
    name: chiwei_test_pg_data
  chiwei_test_mq_data:
    name: chiwei_test_mq_data
  chiwei_test_redis_data:
    name: chiwei_test_redis_data
  chiwei_test_qdrant_data:
    name: chiwei_test_qdrant_data
  chiwei_test_mongo_data:
    name: chiwei_test_mongo_data
```

- [ ] **Step 5: 本地 lint docker-compose 配置**

Run:
```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/feat-dev-workflow-v2-phase-3
docker compose -f infra/test-env/docker-compose.yaml config --quiet
```

Expected: 退出码 0（如果有 env 变量提示，可以临时 `CHIWEI_TEST_PG_PASSWORD=x CHIWEI_TEST_MQ_PASSWORD=x CHIWEI_TEST_REDIS_PASSWORD=x CHIWEI_TEST_QDRANT_API_KEY=x CHIWEI_TEST_MONGO_PASSWORD=x docker compose ... config`，只验语法不验值）。

如果报错（缩进 / 重复 key），修复后重跑。

- [ ] **Step 6: Commit**

```bash
git add infra/test-env/docker-compose.yaml
git commit -m "feat(workflow): phase 3 — chiwei-test qdrant + mongo containers + rabbitmq fixed digest

Add chiwei-test-qdrant (qdrant/qdrant:v1.11.0, port 16333/16334) and
chiwei-test-mongo (mongo:7.0.14-jammy, port 27018) to test-env docker
compose, sibling to existing chiwei-test-postgres/-redis/-rabbitmq.

Also pin chiwei-test-rabbitmq image to harbor digest (was :latest in
prod cpu1 but not committed in Phase 2 — fix the missed commit and
avoid future drift)."
```

---

## Task 3: cpu1 拉起 qdrant + mongo 容器

**Files:** 无文件改动，纯运维

- [ ] **Step 1: ssh cpu1 把 plan 改动同步到 cpu1 上的 git checkout**

cpu1 上仓库位置假设为 `~/chiwei-platform`（参考 Phase 1 约定，如果不是，先 `find ~ -name "docker-compose.yaml" -path "*test-env*"` 定位）：

```bash
ssh cpu1 'cd ~/chiwei-platform && git fetch && git checkout feat/dev-workflow-v2-phase-3 && git pull'
```

Expected: 切到 phase 3 分支，最新 commit 含 Task 2 的 docker-compose 改动。

- [ ] **Step 2: 在 cpu1 上拉取新 image**

```bash
ssh cpu1 'cd ~/chiwei-platform/infra/test-env && docker compose pull chiwei-test-qdrant chiwei-test-mongo'
```

Expected: 成功 pull qdrant/qdrant:v1.11.0 + mongo:7.0.14-jammy。如果某 image 拉不下来（公网封锁），切到 harbor mirror 或换 tag。

- [ ] **Step 3: 设置 env 变量 + 拉起容器**

cpu1 上 `~/chiwei-platform/.env`（或别处放 env 的位置）追加：

```
CHIWEI_TEST_QDRANT_API_KEY=<生成 32 字节 hex 字符串>
CHIWEI_TEST_MONGO_PASSWORD=<生成 32 字节 hex 字符串>
```

生成方式（在 cpu1 上跑）：
```bash
openssl rand -hex 32  # qdrant
openssl rand -hex 16  # mongo
```

把生成的值记到 `infrastructure.md` memory（敏感，不入 git）+ 本步骤记到 plan 临时记忆。

```bash
ssh cpu1 'cd ~/chiwei-platform/infra/test-env && docker compose up -d chiwei-test-qdrant chiwei-test-mongo'
```

Expected: 两个 container 启动成功。

- [ ] **Step 4: 等 healthcheck pass**

```bash
ssh cpu1 'docker ps --filter name=chiwei-test-qdrant --filter name=chiwei-test-mongo --format "table {{.Names}}\t{{.Status}}"'
```

Expected: 两个容器都 `Up X seconds (healthy)`。如果 60s 内仍 `(starting)` 或 `(unhealthy)`，看 logs：

```bash
ssh cpu1 'docker logs --tail 50 chiwei-test-qdrant'
ssh cpu1 'docker logs --tail 50 chiwei-test-mongo'
```

常见问题：mongo 第一次启动初始化 root user 慢；qdrant API_KEY env 没注入（检查 .env 文件被 docker compose 读到）。

- [ ] **Step 5: 从 cpu1 内部验证连通**

```bash
ssh cpu1 'curl -sf http://localhost:16333/readyz -H "api-key: <CHIWEI_TEST_QDRANT_API_KEY>"'
```

Expected: 输出 `all shards are ready` 或类似 ok response。

```bash
ssh cpu1 'docker exec chiwei-test-mongo mongosh --quiet --username chiwei-test --password <CHIWEI_TEST_MONGO_PASSWORD> --authenticationDatabase admin --eval "db.adminCommand({ping: 1})"'
```

Expected: `{ ok: 1 }`。

无 commit（容器拉起不进 git）。

---

## Task 4: 从 k3s prod ns 验证测试容器可达

**Files:** 无文件改动

- [ ] **Step 1: 找一个 prod ns 已部署的 pod，从里面 curl 测试容器**

任选一个 prod pod（如 agent-service），跑 curl：

```bash
make logs APP=agent-service KEYWORD="lifespan" SINCE=5m 2>&1 | head -5  # 先确认 pod 在跑
# 实际查 pod 名要走 /ops pods APP=agent-service LANE=prod
```

```bash
# 用 /ops skill 拿到一个 prod pod 名后：
kubectl exec -n prod <agent-service-pod> -- curl -sf "http://10.37.6.235:16333/readyz" -H "api-key: <CHIWEI_TEST_QDRANT_API_KEY>"
```

**注意**：CLAUDE.md `safety-rules.md` 禁止 kubectl exec 写操作，但允许 read-only 排查（curl 是 read-only）。本步骤仅做连通性测试，无写入。

Expected: 同 Task 3 Step 5 的 readyz 响应。

- [ ] **Step 2: 验证 mongo 连通**

```bash
kubectl exec -n prod <lark-server-pod> -- sh -c "mongosh --quiet --host 10.37.6.235:27018 --username chiwei-test --password '<CHIWEI_TEST_MONGO_PASSWORD>' --authenticationDatabase admin --eval 'db.adminCommand({ping: 1})'"
```

如果 lark-server 容器没 mongosh，跳过；用 Task 6 真实 deploy 验证。

Expected (有 mongosh 时): `{ ok: 1 }`。

- [ ] **Step 3: 记录 baseline qdrant collections（验收用）**

部署 coe-validation 之前先记录 chiwei-test-qdrant 当前 collection 列表，留作 Task 9 的 before-snapshot：

```bash
ssh cpu1 'curl -sf http://localhost:16333/collections -H "api-key: <CHIWEI_TEST_QDRANT_API_KEY>"' | tee /tmp/qdrant-baseline.json
```

Expected: 如果 chiwei-test-qdrant 是 fresh 容器，`{"result":{"collections":[]}, ...}`；如果有历史 deploy 残留，可能含 4 个 messages_* / memory_* collection。**baseline 不为空也没关系，Task 9 会做 diff**。

记录 baseline `vectors_count` 字段为 0 或具体值，Task 9 diff 用。

无 commit。

---

## Task 5: PUT `qdrant` bundle 加 class_overrides + required_keys

**Files:** 无文件改动（PaaS API 操作 paas_engine DB）

- [ ] **Step 0: Capture prod resolved-config baseline（验证零变化的 prerequisite）**

引用 qdrant bundle 的 App 是 agent-service / vectorize-worker。先存 prod resolved-config + prod qdrant collections 作 baseline，Task 5 Step 5 用：

```bash
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/apps/agent-service/resolved-config?lane=prod" "Authorization: Bearer $PAAS_TOKEN" | jq -S '.' > /tmp/agent-service-prod-resolved-BEFORE.json
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/apps/vectorize-worker/resolved-config?lane=prod" "Authorization: Bearer $PAAS_TOKEN" | jq -S '.' > /tmp/vectorize-worker-prod-resolved-BEFORE.json

# 同时 capture prod qdrant collections list（Task 10 Step 3 用）
# 需要 kubectl exec 进 prod agent-service pod
PROD_AS_POD=$(bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/dashboard/api/ops/services/agent-service/pods?lane=prod" "X-API-Key: $DASHBOARD_CC_TOKEN" | jq -r '.data.pods[0].name')
kubectl exec -n prod "$PROD_AS_POD" -- curl -sf "http://qdrant:6333/collections" -H "api-key: $(jq -r '.data.resolved.QDRANT_SERVICE_API_KEY' /tmp/agent-service-prod-resolved-BEFORE.json)" | jq -S '.' > /tmp/prod-qdrant-coll-BEFORE.json
```

Expected: 3 个文件都生成且非空。

- [ ] **Step 1: GET 备份当前 qdrant bundle 完整 JSON**

```bash
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/config-bundles/qdrant" "Authorization: Bearer $PAAS_TOKEN" | tee /tmp/qdrant-bundle-before.json
```

Expected: 200 状态码 + 含 baseline keys（QDRANT_SERVICE_HOST=qdrant 等 4 key）+ `class_overrides: null` + `required_keys: null`。

**如果 baseline keys 跟 ops-db 查到的不一致**（之前 ops-db 看到 4 key 含 QDRANT_API_KEY 死 key），停下来跟用户确认。

- [ ] **Step 2: 构造 PUT body**

复制 `/tmp/qdrant-bundle-before.json`，**保留** baseline `keys`（QDRANT_SERVICE_HOST=qdrant / QDRANT_SERVICE_PORT=6333 / QDRANT_SERVICE_API_KEY=<prod 值> / QDRANT_API_KEY=<prod 值>）不动，**加** `class_overrides` 和 `required_keys`：

```json
{
  "keys": {
    "QDRANT_SERVICE_HOST": "qdrant",
    "QDRANT_SERVICE_PORT": "6333",
    "QDRANT_SERVICE_API_KEY": "<原 prod 值，从 before.json 抄过来>",
    "QDRANT_API_KEY": "<原 prod 值，从 before.json 抄过来>"
  },
  "class_overrides": {
    "coe": {
      "QDRANT_SERVICE_HOST": "10.37.6.235",
      "QDRANT_SERVICE_PORT": "16333",
      "QDRANT_SERVICE_API_KEY": "<CHIWEI_TEST_QDRANT_API_KEY>",
      "QDRANT_API_KEY": "<CHIWEI_TEST_QDRANT_API_KEY>"
    }
  },
  "required_keys": {
    "coe": ["QDRANT_SERVICE_HOST", "QDRANT_SERVICE_PORT", "QDRANT_SERVICE_API_KEY", "QDRANT_API_KEY"]
  }
}
```

保存到 `/tmp/qdrant-bundle-after.json`。

- [ ] **Step 3: PUT 提交**

```bash
bash .claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/config-bundles/qdrant" "$(cat /tmp/qdrant-bundle-after.json)" "Authorization: Bearer $PAAS_TOKEN"
```

Expected: 200 + 返回的 body 含上述新字段。

- [ ] **Step 4: GET 验证生效**

```bash
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/config-bundles/qdrant" "Authorization: Bearer $PAAS_TOKEN" | jq '.data.class_overrides, .data.required_keys'
```

Expected: 输出非空，含 `coe` key。

- [ ] **Step 5: prod resolved-config byte-equal 验证**

agent-service 和 vectorize-worker 引用 qdrant。Diff 它们的 prod resolved-config：

```bash
# 改动前在 Task 4 完成后、Step 1 之前应该提前 capture 这两个 (如果忘了，回去补)
# 假设 capture 路径：/tmp/agent-service-prod-resolved-BEFORE.json + /tmp/vectorize-worker-prod-resolved-BEFORE.json

bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/apps/agent-service/resolved-config?lane=prod" "Authorization: Bearer $PAAS_TOKEN" | jq -S '.' > /tmp/agent-service-prod-resolved-AFTER.json
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/apps/vectorize-worker/resolved-config?lane=prod" "Authorization: Bearer $PAAS_TOKEN" | jq -S '.' > /tmp/vectorize-worker-prod-resolved-AFTER.json

diff /tmp/agent-service-prod-resolved-BEFORE.json /tmp/agent-service-prod-resolved-AFTER.json
diff /tmp/vectorize-worker-prod-resolved-BEFORE.json /tmp/vectorize-worker-prod-resolved-AFTER.json
```

Expected: **两个 diff 输出都为空**（byte-equal）。如果不为空，**立即回滚** —— PUT bundle 把 class_overrides/required_keys 改回 null：

```bash
echo '{"class_overrides":null,"required_keys":null,"keys":{...原值...}}' > /tmp/qdrant-rollback.json
bash .claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/config-bundles/qdrant" "$(cat /tmp/qdrant-rollback.json)" "Authorization: Bearer $PAAS_TOKEN"
```

无 commit（bundle 改动不进 git）。

---

## Task 6: PUT `mongo` bundle 加 class_overrides + required_keys

**Files:** 无文件改动

- [ ] **Step 0: Capture prod resolved-config baseline（mongo 引用方）**

引用 mongo bundle 的 App 是 lark-server / chat-response-worker / recall-worker。Capture 3 个 prod resolved-config + 1 个 prod mongo lark_event count（Task 11 Step 4 用）：

```bash
for app in lark-server chat-response-worker recall-worker; do
  bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/apps/$app/resolved-config?lane=prod" "Authorization: Bearer $PAAS_TOKEN" | jq -S '.' > /tmp/$app-prod-resolved-BEFORE.json
done

# prod mongo lark_event count baseline
PROD_LS_POD=$(bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/dashboard/api/ops/services/lark-server/pods?lane=prod" "X-API-Key: $DASHBOARD_CC_TOKEN" | jq -r '.data.pods[0].name')
kubectl exec -n prod "$PROD_LS_POD" -- mongosh --quiet --eval 'JSON.stringify({count: db.getSiblingDB("chiwei").lark_event.estimatedDocumentCount(), ts: new Date().toISOString()})' > /tmp/prod-mongo-count-BEFORE.json
```

Expected: 4 个文件生成且非空。

- [ ] **Step 1: GET 备份当前 mongo bundle**

```bash
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/config-bundles/mongo" "Authorization: Bearer $PAAS_TOKEN" | tee /tmp/mongo-bundle-before.json
```

Expected: 200 + baseline keys = `{MONGO_HOST=mongodb, MONGO_INITDB_ROOT_USERNAME=chiwei, MONGO_INITDB_ROOT_PASSWORD=<prod 现值>}` + `class_overrides: null` + `required_keys: null`。

- [ ] **Step 2: 构造 PUT body**

```json
{
  "keys": {
    "MONGO_HOST": "mongodb",
    "MONGO_INITDB_ROOT_USERNAME": "chiwei",
    "MONGO_INITDB_ROOT_PASSWORD": "<原 prod 值，从 before.json 抄过来>"
  },
  "class_overrides": {
    "coe": {
      "MONGO_HOST": "10.37.6.235:27018",
      "MONGO_INITDB_ROOT_USERNAME": "chiwei-test",
      "MONGO_INITDB_ROOT_PASSWORD": "<CHIWEI_TEST_MONGO_PASSWORD>"
    }
  },
  "required_keys": {
    "coe": ["MONGO_HOST", "MONGO_INITDB_ROOT_USERNAME", "MONGO_INITDB_ROOT_PASSWORD"]
  }
}
```

保存到 `/tmp/mongo-bundle-after.json`。

- [ ] **Step 3: PUT 提交**

```bash
bash .claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/config-bundles/mongo" "$(cat /tmp/mongo-bundle-after.json)" "Authorization: Bearer $PAAS_TOKEN"
```

Expected: 200。

- [ ] **Step 4: GET 验证生效**

```bash
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/config-bundles/mongo" "Authorization: Bearer $PAAS_TOKEN" | jq '.data.class_overrides, .data.required_keys'
```

Expected: 输出非空，含 `coe` key 含 3 个 key。

- [ ] **Step 5: prod resolved-config byte-equal 验证（3 App）**

```bash
for app in lark-server chat-response-worker recall-worker; do
  bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/apps/$app/resolved-config?lane=prod" "Authorization: Bearer $PAAS_TOKEN" | jq -S '.' > /tmp/$app-prod-resolved-AFTER.json
  echo "=== $app diff ==="
  diff /tmp/$app-prod-resolved-BEFORE.json /tmp/$app-prod-resolved-AFTER.json
done
```

Expected: 三个 diff 都为空。

如果任一非空，**立即回滚**：

```bash
echo '{"keys":<原 keys>,"class_overrides":null,"required_keys":null}' > /tmp/mongo-rollback.json
bash .claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/config-bundles/mongo" "$(cat /tmp/mongo-rollback.json)" "Authorization: Bearer $PAAS_TOKEN"
```

---

## Task 7: 反向测试 RequiredKeys reject

**Files:** 无文件改动（临时操作，测完恢复）

**目的**：验证 RequiredKeys[coe] 真的强制——故意删 class_overrides[coe] 里的一个 key，deploy 期望 HTTP 400。

- [ ] **Step 1: 把 qdrant bundle 的 class_overrides[coe] 临时删一个 key**

PUT body 改为（删 `QDRANT_SERVICE_PORT`）：

```json
{
  ...
  "class_overrides": {
    "coe": {
      "QDRANT_SERVICE_HOST": "10.37.6.235",
      "QDRANT_SERVICE_API_KEY": "<CHIWEI_TEST_QDRANT_API_KEY>",
      "QDRANT_API_KEY": "<CHIWEI_TEST_QDRANT_API_KEY>"
    }
  },
  "required_keys": {
    "coe": ["QDRANT_SERVICE_HOST", "QDRANT_SERVICE_PORT", "QDRANT_SERVICE_API_KEY", "QDRANT_API_KEY"]
  }
}
```

PUT 到 paas-engine。

- [ ] **Step 2: 试 deploy agent-service 到 coe-validation lane**

```bash
make deploy APP=agent-service LANE=coe-validation GIT_REF=feat/dev-workflow-v2-phase-3 2>&1 | tail -20
```

Expected: 部署被 paas-engine reject，HTTP 400，错误信息含 `required key QDRANT_SERVICE_PORT not in class_overrides[coe]` 类似措辞。

- [ ] **Step 3: 立即恢复 qdrant bundle**

把 Task 5 Step 2 的完整 PUT body 重新提交，恢复 class_overrides[coe] 含 4 个 key。

```bash
bash .claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/config-bundles/qdrant" "$(cat /tmp/qdrant-bundle-after.json)" "Authorization: Bearer $PAAS_TOKEN"
```

- [ ] **Step 4: GET 验证恢复**

```bash
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/config-bundles/qdrant" "Authorization: Bearer $PAAS_TOKEN" | jq '.data.class_overrides.coe | keys'
```

Expected: 输出 `["QDRANT_API_KEY", "QDRANT_SERVICE_API_KEY", "QDRANT_SERVICE_HOST", "QDRANT_SERVICE_PORT"]`。

无 commit。

---

## Task 8: grep OSS / TOS 写路径并确认验证不触发

**目的**：spec §6.2 mandatory——验证 coe-validation lane 部署后不会触发 OSS / TOS 写操作（否则可能覆盖 prod 关键文件）。

**Files:** 无文件改动（纯静态分析）

- [ ] **Step 1: grep lark-server 全部 OSS 写 callsites**

```bash
grep -rn "client\.put\|client\.copy\|client\.delete\|\.upload" /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/feat-dev-workflow-v2-phase-3/apps/lark-server/src/infrastructure/integrations/aliyun/ 2>&1
grep -rn "tos.*put\|tos.*upload\|tos.*delete" /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/feat-dev-workflow-v2-phase-3/apps/lark-server/src/ 2>&1
```

列出全部命中行（文件:行号）。

- [ ] **Step 2: 反向 trace 哪些上游 handler 会调到 Step 1 的 callsites**

对 Step 1 的每个 callsite，grep "import 它的文件"：

```bash
# 假设 Step 1 找到 oss.ts 的 uploadFile，grep 谁 import oss / uploadFile
grep -rn "from.*aliyun/oss\|uploadFile\b\|uploadImage\b" /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/feat-dev-workflow-v2-phase-3/apps/lark-server/src/ 2>&1 | head -20
```

确认这些上游 handler 是否会被本次 coe-validation 验证（发飞书文本消息）触发。

- [ ] **Step 3: 列出验证场景 vs OSS 写路径 mapping**

写一段文字记录到 `/tmp/oss-write-analysis.md`：

```
验证场景：发文本消息到飞书 dev bot（绑定到 coe-validation lane）
触发链路：
  - lark-proxy /webhook → lark-server /api/internal/lark-event (写 lark_event 到 mongo) → safety_check → vectorize → recall

OSS/TOS 写路径分析：
  - <list step 1 找到的每条>
  - 触发条件: <描述>
  - 本次验证是否触发: 是 / 否
```

Step 1+2+3 走完，确认**所有 OSS/TOS 写路径都不被本次纯文本消息验证触发**。

如果有任一会触发，**停止 plan**，跟用户沟通是否要：(a) 改用更窄的验证场景 (b) 接受这次 OSS 写入 (c) 加 lane prefix path 缓解。

- [ ] **Step 4: 把 `/tmp/oss-write-analysis.md` 加进 plan 临时记忆，作为 ship 前留档证据**

无 commit（分析文档不入 git，但要保留到 PR 描述）。

---

## Task 9: 端到端部 5 App 到 coe-validation lane

**Files:** 无文件改动

- [ ] **Step 1: capture coe-validation 部署前的 chiwei-test 状态作 baseline**

```bash
# qdrant collections baseline
ssh cpu1 'curl -sf http://localhost:16333/collections -H "api-key: <CHIWEI_TEST_QDRANT_API_KEY>"' > /tmp/qdrant-coll-before.json

# mongo lark_event count baseline
ssh cpu1 'docker exec chiwei-test-mongo mongosh --quiet --username chiwei-test --password "<CHIWEI_TEST_MONGO_PASSWORD>" --authenticationDatabase admin chiwei --eval "db.lark_event.countDocuments()"' > /tmp/mongo-count-before.txt

# 记录 deploy 时间 T0
date -u +"%Y-%m-%dT%H:%M:%SZ" > /tmp/deploy-T0.txt
```

- [ ] **Step 2: 部 agent-service 到 coe-validation**

```bash
make deploy APP=agent-service LANE=coe-validation GIT_REF=feat/dev-workflow-v2-phase-3 2>&1 | tee /tmp/deploy-agent-service.log
```

Expected: build 完 → release 成功，pod 启动。

如果 build/release 失败，根据 spec §7 风险 → 回滚（Task 7 Step 3 同款）+ 停下来跟用户沟通。

- [ ] **Step 3: 同镜像同步 release vectorize-worker**

agent-service 和 vectorize-worker 是同一镜像（CLAUDE.md "一镜像多服务"）。Step 2 build 出来的镜像版本号用同款 release：

```bash
# 拿到 step 2 build 出来的版本号
make latest-build APP=agent-service 2>&1 | grep -E "version|tag"

# 用同版本 release vectorize-worker
make release APP=vectorize-worker LANE=coe-validation VERSION=<step 2 build 出来的版本号> 2>&1 | tail -5
```

Expected: 同样 release 成功。

- [ ] **Step 4: 部 lark-server + 同步 release chat-response-worker / recall-worker**

```bash
make deploy APP=lark-server LANE=coe-validation GIT_REF=feat/dev-workflow-v2-phase-3 2>&1 | tee /tmp/deploy-lark-server.log

# 拿版本号
make latest-build APP=lark-server 2>&1 | grep -E "version|tag"

# 同步 release
make release APP=chat-response-worker LANE=coe-validation VERSION=<lark-server 版本号>
make release APP=recall-worker LANE=coe-validation VERSION=<lark-server 版本号>
```

- [ ] **Step 5: 等 pods ready**

```bash
# 用 /ops skill
bash .claude/skills/api-test/scripts/http.sh GET "$PAAS_API/dashboard/api/ops/services/agent-service/pods?lane=coe-validation" "X-API-Key: $DASHBOARD_CC_TOKEN"
```

对 5 个 App 都查一遍，确认 pods Running + Ready。

如果某 pod CrashLoopBackOff，看 logs：

```bash
make logs APP=<app> KEYWORD="lifespan\|error" SINCE=3m
```

常见问题：(a) bundle 派的 chiwei-test endpoint 不通 → 回 Task 4 验证 (b) RequiredKeys reject 时 paas-engine 不会让 release 成功，应在 Step 2/4 就报错。

无 commit。

---

## Task 10: 验证 Qdrant `init_collections` 真打 chiwei-test

**Files:** 无文件改动

- [ ] **Step 1: deploy 后再 snapshot qdrant collections**

```bash
ssh cpu1 'curl -sf http://localhost:16333/collections -H "api-key: <CHIWEI_TEST_QDRANT_API_KEY>"' > /tmp/qdrant-coll-after.json
```

- [ ] **Step 2: diff baseline vs after**

```bash
diff <(jq -S . /tmp/qdrant-coll-before.json) <(jq -S . /tmp/qdrant-coll-after.json)
```

Expected: diff 显示 after 比 before **多出**或**已含**这 4 个 collection：
- `messages_recall`
- `messages_cluster`
- `memory_fragment`
- `memory_abstract`

如果 baseline 是空、after 含 4 个 → ✅
如果 baseline 已含 4 个、after 仍是 4 个 → 需要额外证据（看 agent-service log）：

```bash
make logs APP=agent-service LANE=coe-validation KEYWORD="Creating collection\|init_collections\|qdrant" SINCE=5m | tail -30
```

Expected log 含 "Creating collection messages_recall" 或 "Collection already exists" 等明确显示 agent-service 在跟 **chiwei-test qdrant**（10.37.6.235:16333）通信的字符串。

- [ ] **Step 3: 验证 prod qdrant 零变化**

```bash
# prod qdrant 地址：内部 DNS qdrant:6333（从 prod 内访问），开发机不能直接连，要 kubectl exec
kubectl exec -n prod <prod-agent-service-pod> -- curl -sf "http://qdrant:6333/collections" -H "api-key: <prod QDRANT_SERVICE_API_KEY>" > /tmp/prod-qdrant-coll.json

# 跟改动前的 prod qdrant collection list 比对（最好在 Task 4 Step 3 之前先 capture）
diff /tmp/prod-qdrant-coll-BEFORE.json /tmp/prod-qdrant-coll.json
```

Expected: diff 为空（prod qdrant 无新 collection / 无 vectors_count 变化）。

无 commit。

---

## Task 11: 验证 Mongo `insertEvent` 真打 chiwei-test

**Files:** 无文件改动

- [ ] **Step 1: 绑定 dev bot 到 coe-validation lane**

```bash
bash .claude/skills/api-test/scripts/http.sh POST "$PAAS_API/dashboard/api/ops/lane-bindings" '{"type":"bot","key":"dev","lane":"coe-validation"}' "X-API-Key: $DASHBOARD_CC_TOKEN"
```

Expected: 200。

- [ ] **Step 2: 飞书发一条测试消息到 dev bot**

跟用户协商一句简短测试文本（如"phase3 test 2026-05-12 hh:mm"），用户在飞书 dev bot 私聊里发。

等 10s 让消息流走完 lark-proxy → lark-server → mongo insertEvent 链路。

- [ ] **Step 3: 查 chiwei-test-mongo lark_event 最新一条**

```bash
ssh cpu1 'docker exec chiwei-test-mongo mongosh --quiet --username chiwei-test --password "<CHIWEI_TEST_MONGO_PASSWORD>" --authenticationDatabase admin chiwei --eval "JSON.stringify(db.lark_event.findOne({}, {sort:{_id:-1}}))"'
```

Expected: 输出最新一条 event 的 JSON，其 `_id` ObjectId 内嵌时间戳 ≥ T0（Task 9 Step 1 记录的）。

ObjectId 时间戳验证：
```bash
# ObjectId 前 8 个 hex 字符是 unix 秒
# 例如 "_id":"6442a3b8..." → 0x6442a3b8 = 1682138552 (unix秒)
# 跟 /tmp/deploy-T0.txt 比对
```

如果 lark_event 为空（findOne 返回 null）或最新一条早于 T0，说明 mongo 写入没走通：
- 看 lark-server logs：`make logs APP=lark-server LANE=coe-validation KEYWORD="insertEvent\|MongoDB" SINCE=5m`
- 看 chiwei-test-mongo logs：`ssh cpu1 'docker logs --tail 50 chiwei-test-mongo'`

- [ ] **Step 4: 验证 prod mongo 零变化**

记录 deploy 时间 T0 到现在，prod mongo lark_event count 增量应该来自 prod bot 流量（**不**应该来自 dev bot —— dev bot 全部流量被绑到 coe-validation lane）。

```bash
# 从 prod 内部访问 prod mongo（这里也是 read-only kubectl exec）
kubectl exec -n prod <prod-lark-server-pod> -- sh -c "mongosh --quiet --eval 'db.getSiblingDB(\"chiwei\").lark_event.find({_id: {\$gt: ObjectId(\"<T0 的 ObjectId\")}}).count()'"
```

Expected: count 来自的 event 都不带 dev bot app_id（dev bot 流量被 coe lane 接走了）。

如果 prod mongo 出现 dev bot event，说明 lark-proxy 路由失败 fallback 到 prod 了 —— 这是另一个 bug，停下来诊断。

无 commit。

---

## Task 12: 清理 + 提 PR

**Files:** 无新文件改动（提交已 commit 的）

- [ ] **Step 1: 解绑 dev bot**

```bash
bash .claude/skills/api-test/scripts/http.sh DELETE "$PAAS_API/dashboard/api/ops/lane-bindings/bot/dev" "X-API-Key: $DASHBOARD_CC_TOKEN"
```

Expected: 200，dev bot 解除绑定。**用户 explicit 反馈 `feedback_no_undeploy_without_permission.md`**——本步只解 binding，不 undeploy。

- [ ] **Step 2: 跟用户确认 coe-validation lane 是否需要 undeploy**

跟用户说："验证完毕：qdrant collections diff + mongo insertEvent timestamp 都符合预期 + prod 零变化。dev bot 已解绑。是否 undeploy 5 个 App 释放 coe-validation lane 资源？"

等用户明确说 "undeploy"。

- [ ] **Step 3（用户确认后才做）: undeploy 5 App**

```bash
for app in agent-service vectorize-worker lark-server chat-response-worker recall-worker; do
  make undeploy APP=$app LANE=coe-validation
done
```

- [ ] **Step 4: 看分支所有 commits + 改动文件**

```bash
git log main..HEAD --oneline
git diff main...HEAD --stat
```

Expected: 仅 2 个 commit（Task 2 的 docker-compose 改动 + 之前 spec commit）+ 仅改 `docs/superpowers/specs/2026-05-12-...md` 和 `infra/test-env/docker-compose.yaml`。

**如果出现意外文件改动**（按 `merge-and-ship.md` 铁律），停下来跟用户列出来确认。

- [ ] **Step 5: 提 PR**

```bash
ghc pr create --title "feat(workflow): dev workflow v2 phase 3 — qdrant/mongo class_overrides + missed commit" --body "$(cat <<'EOF'
## Summary

Phase 3 of dev workflow v2 (test env isolation): extend existing
`qdrant` and `mongo` ConfigBundles with `class_overrides[coe]` +
`required_keys[coe]` so coe-* lane deployments are routed to
chiwei-test containers instead of prod qdrant/mongo. Adds new test
containers `chiwei-test-qdrant` and `chiwei-test-mongo` to
infra/test-env docker-compose, and pins `chiwei-test-rabbitmq` image
to a fixed digest (fixes Phase 2's missed commit drift).

External SaaS (OSS/TOS/Langfuse/AI provider) explicitly out of scope —
see spec §6 for mitigation rules.

## Spec & Plan

- Spec: `docs/superpowers/specs/2026-05-12-dev-workflow-v2-phase-3-design.md`
- Plan: `docs/superpowers/plans/2026-05-12-dev-workflow-v2-phase-3.md`

## Test Plan

- [x] docker compose config syntax check passed
- [x] chiwei-test-qdrant + chiwei-test-mongo healthy on cpu1
- [x] qdrant/mongo bundle PUT — prod resolved-config byte-equal (no
      prod behavior change)
- [x] RequiredKeys[coe] rejects missing key (HTTP 400)
- [x] coe-validation lane deployed all 5 apps successfully
- [x] agent-service init_collections wrote to chiwei-test-qdrant
      (verified via collections list diff)
- [x] lark-server insertEvent wrote to chiwei-test-mongo (verified via
      ObjectId timestamp >= deploy T0)
- [x] OSS/TOS write callsites grep'd — none triggered by test scenario
- [x] prod qdrant + prod mongo zero change post-validation
- [x] dev bot binding removed, validation lane cleaned up
EOF
)"
```

记录 PR URL，等用户合码。**禁止自行 merge**（`feedback_no_unauthorized_pr.md` 红线）。

---

## 整体回滚预案

如任何 Task 出现 prod 行为变化：

1. **bundle 回滚**：PUT qdrant + mongo bundle 把 `class_overrides` 和 `required_keys` 改回 null（baseline keys 不动）
2. **测试容器停止**：`ssh cpu1 'cd ~/chiwei-platform/infra/test-env && docker compose stop chiwei-test-qdrant chiwei-test-mongo'`
3. **docker-compose.yaml git 回滚**：revert Task 2 的 commit（如果已 push 且 PR 已提，先撤 PR）
4. **paas-engine 自身代码无改动，无需回滚**

---

## Spec Coverage Check

对照 `docs/superpowers/specs/2026-05-12-dev-workflow-v2-phase-3-design.md` 各 §：

| Spec § | 覆盖 Task |
|---|---|
| §2 范围 | Task 1-12 |
| §3.1 测试容器 | Task 1-3 |
| §3.2 bundle 设计 | Task 5-6 |
| §3.3 App 引用无需改 | Task 5-6（不动 config_bundles） |
| §3.4 rabbitmq 漏 commit | Task 1-2 |
| §3.5 零业务代码改动 | 整个 plan 不动业务代码 |
| §4 实施顺序 | Task 1-12 一致 |
| §5 验收 | Task 4 / Task 7 / Task 10 / Task 11 |
| §6.2 OSS/TOS mandatory 缓解 | Task 8 |
| §7 风险 + 回滚 | "整体回滚预案" 段 |
| §8 验证后 memory | Task 12 完成后另起 memory update（不在 plan 内）|
