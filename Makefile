.PHONY: deploy self-deploy release undeploy status latest-build lane-bind lane-unbind lane-bindings

# ---------- 参数 ----------
# APP        — 应用名（必填），对应 apps/<APP> 和 PaaS 注册的应用名
# VERSION    — 显式指定版本号（可选）
# BUMP       — 版本递进："major"/"minor"/"patch"/""（可选）
# GIT_REF    — 构建分支/tag/commit，默认当前分支
# LANE       — 部署泳道，默认 prod

GIT_REF  ?= $(shell git rev-parse --abbrev-ref HEAD)
VERSION  ?=
BUMP     ?=
LANE     ?= prod

define require_app
	$(if $(APP),,$(error APP 未指定。用法: make $@ APP=<应用名>))
endef

define require_main_for_prod
	@if [ "$(LANE)" = "prod" ] && [ "$(GIT_REF)" != "main" ]; then \
		echo ">>> 错误: 禁止将非 main 分支 ($(GIT_REF)) 部署到 prod 泳道"; \
		echo ">>>   请先合并到 main，或指定 LANE=<泳道名> 部署到非 prod 泳道"; \
		exit 1; \
	fi
endef

define require_pushed
	@if git show-ref --verify --quiet refs/heads/$(GIT_REF) 2>/dev/null; then \
		if ! git show-ref --verify --quiet refs/remotes/origin/$(GIT_REF) 2>/dev/null; then \
			echo ">>> 错误: 分支 $(GIT_REF) 未推送到远端，请先 git push"; \
			exit 1; \
		fi; \
		LOCAL_SHA=$$(git rev-parse refs/heads/$(GIT_REF)); \
		REMOTE_SHA=$$(git rev-parse refs/remotes/origin/$(GIT_REF)); \
		if [ "$$LOCAL_SHA" != "$$REMOTE_SHA" ]; then \
			echo ">>> 错误: 分支 $(GIT_REF) 有未推送的 commit，请先 git push"; \
			echo ">>>   本地: $$LOCAL_SHA"; \
			echo ">>>   远端: $$REMOTE_SHA"; \
			exit 1; \
		fi; \
		CURRENT_BRANCH=$$(git rev-parse --abbrev-ref HEAD); \
		if [ "$(GIT_REF)" = "$$CURRENT_BRANCH" ]; then \
			if ! git diff --quiet HEAD 2>/dev/null || ! git diff --cached --quiet HEAD 2>/dev/null; then \
				echo ">>> 警告: 工作区有未提交的改动，不会包含在构建中"; \
			fi; \
		fi; \
	fi
endef

# ---------- 命令 ----------

## 一键部署：构建 → 等待 → 发布到指定泳道
## 用法: make deploy APP=my-service [LANE=dev] [BUMP=minor] [VERSION=2.0.0.1]
deploy:
	@$(call require_app)
	$(call require_main_for_prod)
	$(call require_pushed)
	@echo ">>> 部署 $(APP): $(GIT_REF) -> $(LANE)"
	@BUILD_RESP=$$(curl -sf -X POST $(PAAS_API)/api/paas/apps/$(APP)/builds/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"git_ref":"$(GIT_REF)","version":"$(VERSION)","bump":"$(BUMP)"}') && \
	BUILD_ID=$$(echo "$$BUILD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])") && \
	BUILD_VER=$$(echo "$$BUILD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['version'])") && \
	echo ">>> 构建已触发: $$BUILD_ID (版本: $$BUILD_VER)" && \
	while true; do \
		STATUS=$$(curl -sf $(PAAS_API)/api/paas/apps/$(APP)/builds/$$BUILD_ID/ \
			-H 'X-API-Key: $(PAAS_TOKEN)' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])"); \
		echo "    状态: $$STATUS"; \
		case $$STATUS in \
			succeeded) echo ">>> 构建成功"; break;; \
			failed)    echo ">>> 构建失败"; exit 1;; \
			cancelled) echo ">>> 构建已取消"; exit 1;; \
		esac; \
		sleep 5; \
	done && \
	ACTUAL_TAG=$$(curl -sf $(PAAS_API)/api/paas/apps/$(APP)/builds/$$BUILD_ID/ \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['data']['image_tag'].rsplit(':',1)[-1])") && \
	echo ">>> 发布 $(APP) -> $(LANE), tag: $$ACTUAL_TAG" && \
	curl -sf -X POST $(PAAS_API)/api/paas/releases/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d "{\"app_name\":\"$(APP)\",\"lane\":\"$(LANE)\",\"image_tag\":\"$$ACTUAL_TAG\",\"replicas\":1}" \
		| python3 -m json.tool && \
	echo ">>> 部署完成"

## paas-engine 蓝绿自部署：构建 → 等待 → prod → blue
## 用法: make self-deploy [BUMP=minor]
self-deploy:
	$(call require_main_for_prod)
	$(call require_pushed)
	@echo ">>> 蓝绿自部署 paas-engine: $(GIT_REF) -> prod+blue"
	@BUILD_RESP=$$(curl -sf -X POST $(PAAS_API)/api/paas/apps/paas-engine/builds/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"git_ref":"$(GIT_REF)","version":"$(VERSION)","bump":"$(BUMP)"}') && \
	BUILD_ID=$$(echo "$$BUILD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])") && \
	BUILD_VER=$$(echo "$$BUILD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['version'])") && \
	echo ">>> 构建已触发: $$BUILD_ID (版本: $$BUILD_VER)" && \
	while true; do \
		STATUS=$$(curl -sf $(PAAS_API)/api/paas/apps/paas-engine/builds/$$BUILD_ID/ \
			-H 'X-API-Key: $(PAAS_TOKEN)' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])"); \
		echo "    状态: $$STATUS"; \
		case $$STATUS in \
			succeeded) echo ">>> 构建成功"; break;; \
			failed)    echo ">>> 构建失败"; exit 1;; \
			cancelled) echo ">>> 构建已取消"; exit 1;; \
		esac; \
		sleep 5; \
	done && \
	ACTUAL_TAG=$$(curl -sf $(PAAS_API)/api/paas/apps/paas-engine/builds/$$BUILD_ID/ \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['data']['image_tag'].rsplit(':',1)[-1])") && \
	echo ">>> 发布 paas-engine -> prod, tag: $$ACTUAL_TAG" && \
	curl -sf -X POST $(PAAS_API)/api/paas/releases/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d "{\"app_name\":\"paas-engine\",\"lane\":\"prod\",\"image_tag\":\"$$ACTUAL_TAG\",\"replicas\":1}" \
		| python3 -m json.tool && \
	echo ">>> 等待 prod 泳道就绪..." && sleep 10 && \
	echo ">>> 发布 paas-engine -> blue, tag: $$ACTUAL_TAG" && \
	curl -sf -X POST $(PAAS_API)/api/paas/releases/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d "{\"app_name\":\"paas-engine\",\"lane\":\"blue\",\"image_tag\":\"$$ACTUAL_TAG\",\"replicas\":1}" \
		| python3 -m json.tool && \
	echo ">>> 蓝绿自部署完成"

## 仅发布（不构建），用于切换泳道/回滚
## 用法: make release APP=xxx LANE=yyy VERSION=1.0.0.5
release:
	@$(call require_app)
	$(if $(VERSION),,$(error VERSION 未指定。用法: make release APP=<app> LANE=<lane> VERSION=<version>))
	$(if $(LANE),,$(error LANE 未指定))
	@echo ">>> 发布 $(APP) -> $(LANE), 版本: $(VERSION)"
	@curl -sf -X POST $(PAAS_API)/api/paas/releases/ \
	  -H 'Content-Type: application/json' \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  -d '{"app_name":"$(APP)","lane":"$(LANE)","image_tag":"$(VERSION)","replicas":1}' \
	  | python3 -m json.tool

## 按 app+lane 删除 Release
## 用法: make undeploy APP=xxx LANE=yyy
undeploy:
	@$(call require_app)
	$(if $(LANE),,$(error LANE 未指定))
	@echo ">>> 删除 $(APP) 的 $(LANE) 泳道 Release"
	@curl -sf -X DELETE "$(PAAS_API)/api/paas/releases/?app=$(APP)&lane=$(LANE)" \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -m json.tool

## 查看状态（不传 APP 看全部，传 APP 看单应用）
## 用法: make status [APP=xxx]
status:
	@if [ -n "$(APP)" ]; then \
		echo ">>> $(APP) 泳道状态"; \
		curl -sf "$(PAAS_API)/api/paas/releases/?app=$(APP)" \
			-H 'X-API-Key: $(PAAS_TOKEN)' \
			| python3 -c "import sys,json; [print(f\"  {r['lane']:10s} | {r['status']:10s} | {r['image']}\") for r in json.load(sys.stdin).get('data', [])]"; \
	else \
		echo ">>> 全部 Release 状态"; \
		curl -sf "$(PAAS_API)/api/paas/releases/" \
			-H 'X-API-Key: $(PAAS_TOKEN)' \
			| python3 -c "import sys,json; [print(f\"  {r['app_name']:20s} | {r['lane']:10s} | {r['status']:10s} | {r['image']}\") for r in json.load(sys.stdin).get('data', [])]"; \
	fi

## 查看最近成功构建
## 用法: make latest-build APP=xxx
latest-build:
	@$(call require_app)
	@echo ">>> $(APP) 最近成功构建"
	@curl -sf "$(PAAS_API)/api/paas/apps/$(APP)/builds/latest" \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -m json.tool

# ---------- 泳道绑定 ----------

## 绑定 bot/chat 到泳道
## 用法: make lane-bind TYPE=bot KEY=my-bot LANE=feat-test
lane-bind:
	$(if $(TYPE),,$(error TYPE 未指定（bot 或 chat）))
	$(if $(KEY),,$(error KEY 未指定))
	$(if $(LANE),,$(error LANE 未指定))
	@echo ">>> 绑定 $(TYPE):$(KEY) -> $(LANE)"
	@curl -sf -X POST $(PAAS_API)/api/lark/lane-bindings \
	  -H 'Content-Type: application/json' \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  -d '{"route_type":"$(TYPE)","route_key":"$(KEY)","lane_name":"$(LANE)"}' \
	  | python3 -m json.tool

## 解绑 bot/chat 的泳道
## 用法: make lane-unbind TYPE=bot KEY=my-bot
lane-unbind:
	$(if $(TYPE),,$(error TYPE 未指定（bot 或 chat）))
	$(if $(KEY),,$(error KEY 未指定))
	@echo ">>> 解绑 $(TYPE):$(KEY)"
	@curl -sf -X DELETE "$(PAAS_API)/api/lark/lane-bindings?type=$(TYPE)&key=$(KEY)" \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -m json.tool

## 列出所有活跃泳道绑定
## 用法: make lane-bindings
lane-bindings:
	@echo ">>> 活跃泳道绑定"
	@curl -sf $(PAAS_API)/api/lark/lane-bindings \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -c "import sys,json; [print(f\"  {r['route_type']:6s} | {r['route_key']:30s} | {r['lane_name']}\") for r in json.load(sys.stdin).get('data', [])]"
