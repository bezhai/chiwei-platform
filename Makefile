.PHONY: deploy self-deploy release undeploy status latest-build

# 从 .env 加载配置（PAAS_API, PAAS_TOKEN, REGISTRY 等）
-include .env

# ---------- 参数 ----------
# APP        — 应用名（必填），对应 apps/<APP> 和 PaaS 注册的应用名
# TAG        — 镜像 tag，默认 git short hash
# GIT_REF    — 构建分支/tag/commit，默认当前分支
# LANE       — 部署泳道，默认 prod

GIT_REF  ?= $(shell git rev-parse --abbrev-ref HEAD)
GIT_SHORT := $(shell git rev-parse --short HEAD)
TAG      ?= $(GIT_SHORT)
LANE     ?= prod

define require_app
	$(if $(APP),,$(error APP 未指定。用法: make $@ APP=<应用名>))
endef

# ---------- 命令 ----------

## 一键部署：构建 → 等待 → 发布到指定泳道
## 用法: make deploy APP=my-service [LANE=dev]
deploy:
	@$(call require_app)
	@echo ">>> 部署 $(APP): $(GIT_REF) -> $(TAG) -> $(LANE)"
	@BUILD_ID=$$(curl -sf -X POST $(PAAS_API)/api/v1/apps/$(APP)/builds/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"git_ref":"$(GIT_REF)","image_tag":"$(TAG)"}' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])") && \
	echo ">>> 构建已触发: $$BUILD_ID" && \
	while true; do \
		STATUS=$$(curl -sf $(PAAS_API)/api/v1/apps/$(APP)/builds/$$BUILD_ID/ \
			-H 'X-API-Key: $(PAAS_TOKEN)' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])"); \
		echo "    状态: $$STATUS"; \
		case $$STATUS in \
			succeeded) echo ">>> 构建成功"; break;; \
			failed)    echo ">>> 构建失败"; exit 1;; \
			cancelled) echo ">>> 构建已取消"; exit 1;; \
		esac; \
		sleep 5; \
	done && \
	echo ">>> 发布 $(APP) -> $(LANE), tag: $(TAG)" && \
	curl -sf -X POST $(PAAS_API)/api/v1/releases/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"app_name":"$(APP)","lane":"$(LANE)","image_tag":"$(TAG)","replicas":1}' \
		| python3 -m json.tool && \
	echo ">>> 部署完成"

## paas-engine 蓝绿自部署：构建 → 等待 → prod → blue
## 用法: make self-deploy
self-deploy:
	@echo ">>> 蓝绿自部署 paas-engine: $(GIT_REF) -> $(TAG)"
	@BUILD_ID=$$(curl -sf -X POST $(PAAS_API)/api/v1/apps/paas-engine/builds/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"git_ref":"$(GIT_REF)","image_tag":"$(TAG)"}' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])") && \
	echo ">>> 构建已触发: $$BUILD_ID" && \
	while true; do \
		STATUS=$$(curl -sf $(PAAS_API)/api/v1/apps/paas-engine/builds/$$BUILD_ID/ \
			-H 'X-API-Key: $(PAAS_TOKEN)' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])"); \
		echo "    状态: $$STATUS"; \
		case $$STATUS in \
			succeeded) echo ">>> 构建成功"; break;; \
			failed)    echo ">>> 构建失败"; exit 1;; \
			cancelled) echo ">>> 构建已取消"; exit 1;; \
		esac; \
		sleep 5; \
	done && \
	echo ">>> 发布 paas-engine -> prod, tag: $(TAG)" && \
	curl -sf -X POST $(PAAS_API)/api/v1/releases/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"app_name":"paas-engine","lane":"prod","image_tag":"$(TAG)","replicas":1}' \
		| python3 -m json.tool && \
	echo ">>> 等待 prod 泳道就绪..." && sleep 10 && \
	echo ">>> 发布 paas-engine -> blue, tag: $(TAG)" && \
	curl -sf -X POST $(PAAS_API)/api/v1/releases/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"app_name":"paas-engine","lane":"blue","image_tag":"$(TAG)","replicas":1}' \
		| python3 -m json.tool && \
	echo ">>> 蓝绿自部署完成"

## 仅发布（不构建），用于切换泳道/回滚
## 用法: make release APP=xxx LANE=yyy [TAG=zzz]
release:
	@$(call require_app)
	$(if $(LANE),,$(error LANE 未指定))
	@echo ">>> 发布 $(APP) -> $(LANE), tag: $(TAG)"
	@curl -sf -X POST $(PAAS_API)/api/v1/releases/ \
	  -H 'Content-Type: application/json' \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  -d '{"app_name":"$(APP)","lane":"$(LANE)","image_tag":"$(TAG)","replicas":1}' \
	  | python3 -m json.tool

## 按 app+lane 删除 Release
## 用法: make undeploy APP=xxx LANE=yyy
undeploy:
	@$(call require_app)
	$(if $(LANE),,$(error LANE 未指定))
	@echo ">>> 删除 $(APP) 的 $(LANE) 泳道 Release"
	@curl -sf -X DELETE "$(PAAS_API)/api/v1/releases/?app=$(APP)&lane=$(LANE)" \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -m json.tool

## 查看状态（不传 APP 看全部，传 APP 看单应用）
## 用法: make status [APP=xxx]
status:
	@if [ -n "$(APP)" ]; then \
		echo ">>> $(APP) 泳道状态"; \
		curl -sf "$(PAAS_API)/api/v1/releases/?app=$(APP)" \
			-H 'X-API-Key: $(PAAS_TOKEN)' \
			| python3 -c "import sys,json; [print(f\"  {r['lane']:10s} | {r['status']:10s} | {r['image']}\") for r in json.load(sys.stdin).get('data', [])]"; \
	else \
		echo ">>> 全部 Release 状态"; \
		curl -sf "$(PAAS_API)/api/v1/releases/" \
			-H 'X-API-Key: $(PAAS_TOKEN)' \
			| python3 -c "import sys,json; [print(f\"  {r['app_name']:20s} | {r['lane']:10s} | {r['status']:10s} | {r['image']}\") for r in json.load(sys.stdin).get('data', [])]"; \
	fi

## 查看最近成功构建
## 用法: make latest-build APP=xxx
latest-build:
	@$(call require_app)
	@echo ">>> $(APP) 最近成功构建"
	@curl -sf "$(PAAS_API)/api/v1/apps/$(APP)/builds/latest" \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -m json.tool
