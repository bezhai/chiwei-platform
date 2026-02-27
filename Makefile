.PHONY: build build-status build-wait status release deploy self-deploy

# 从 .env 加载配置（PAAS_API, PAAS_TOKEN, REGISTRY 等）
-include .env

# ---------- 参数 ----------
# APP        — 应用名（必填），对应 apps/<APP> 和 PaaS 注册的应用名
# REPO       — ImageRepo 名，默认与 APP 相同
# TAG        — 镜像 tag，默认 git short hash
# GIT_REF    — 构建分支/tag/commit，默认当前分支
# BUILD_ID   — build-status / build-wait 需要
# LANE       — release 需要

GIT_REF  ?= $(shell git rev-parse --short HEAD)
GIT_SHORT := $(shell git rev-parse --short HEAD)
TAG      ?= $(GIT_SHORT)
REPO     ?= $(APP)

define require_app
	$(if $(APP),,$(error APP 未指定。用法: make $@ APP=<应用名>))
endef

# ---------- 通用命令 ----------

## 触发远程构建
build:
	@$(call require_app)
	@echo ">>> 构建 $(REPO): $(GIT_REF) -> $(TAG)"
	@curl -sf -X POST $(PAAS_API)/api/v1/image-repos/$(REPO)/builds/ \
	  -H 'Content-Type: application/json' \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  -d '{"git_ref":"$(GIT_REF)","image_tag":"$(TAG)"}' \
	  | python3 -m json.tool

## 查看构建状态
build-status:
	@$(call require_app)
	$(if $(BUILD_ID),,$(error BUILD_ID 未指定))
	@curl -sf $(PAAS_API)/api/v1/image-repos/$(REPO)/builds/$(BUILD_ID)/ \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  | python3 -m json.tool

## 轮询等待构建完成
build-wait:
	@$(call require_app)
	$(if $(BUILD_ID),,$(error BUILD_ID 未指定))
	@echo ">>> 等待构建 $(BUILD_ID) 完成..."
	@while true; do \
		STATUS=$$(curl -sf $(PAAS_API)/api/v1/image-repos/$(REPO)/builds/$(BUILD_ID)/ \
			-H 'X-API-Key: $(PAAS_TOKEN)' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])"); \
		echo "    状态: $$STATUS"; \
		case $$STATUS in \
			succeeded) echo ">>> 构建成功"; exit 0;; \
			failed)    echo ">>> 构建失败"; exit 1;; \
			cancelled) echo ">>> 构建已取消"; exit 1;; \
		esac; \
		sleep 5; \
	done

## 查看应用各泳道状态
status:
	@$(call require_app)
	@echo ">>> $(APP) 泳道状态"
	@curl -sf "$(PAAS_API)/api/v1/releases?app=$(APP)" \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		| python3 -c "import sys,json; [print(f\"  {r['lane']:6s} | {r['status']:10s} | {r['image']}\") for r in json.load(sys.stdin).get('data', [])]"

## 发布到指定泳道
release:
	@$(call require_app)
	$(if $(LANE),,$(error LANE 未指定))
	@echo ">>> 发布 $(APP) -> $(LANE), tag: $(TAG)"
	@curl -sf -X POST $(PAAS_API)/api/v1/releases/ \
	  -H 'Content-Type: application/json' \
	  -H 'X-API-Key: $(PAAS_TOKEN)' \
	  -d '{"app_name":"$(APP)","lane":"$(LANE)","image_tag":"$(TAG)","replicas":1}' \
	  | python3 -m json.tool

## 普通服务一键部署：构建 → 等待 → release 到 prod
## 用法: make deploy APP=my-service [REPO=my-repo]
deploy:
	@$(call require_app)
	@echo ">>> 部署 $(APP): $(GIT_REF) -> $(TAG)"
	@BUILD_ID=$$(curl -sf -X POST $(PAAS_API)/api/v1/image-repos/$(REPO)/builds/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"git_ref":"$(GIT_REF)","image_tag":"$(TAG)"}' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])") && \
	echo ">>> 构建已触发: $$BUILD_ID" && \
	$(MAKE) build-wait APP=$(APP) REPO=$(REPO) BUILD_ID=$$BUILD_ID && \
	$(MAKE) release APP=$(APP) LANE=prod TAG=$(TAG) && \
	echo ">>> 部署完成"

## paas-engine 自部署（蓝绿）：构建 → 等待 → 对面泳道 → 本泳道
## 用法: make self-deploy
self-deploy:
	@echo ">>> 蓝绿自部署 paas-engine: $(GIT_REF) -> $(TAG)"
	@BUILD_ID=$$(curl -sf -X POST $(PAAS_API)/api/v1/image-repos/paas-engine/builds/ \
		-H 'Content-Type: application/json' \
		-H 'X-API-Key: $(PAAS_TOKEN)' \
		-d '{"git_ref":"$(GIT_REF)","image_tag":"$(TAG)"}' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])") && \
	echo ">>> 构建已触发: $$BUILD_ID" && \
	$(MAKE) build-wait APP=paas-engine REPO=paas-engine BUILD_ID=$$BUILD_ID && \
	$(MAKE) release APP=paas-engine LANE=prod TAG=$(TAG) && \
	echo ">>> 等待 prod 泳道就绪..." && sleep 10 && \
	$(MAKE) release APP=paas-engine LANE=blue TAG=$(TAG) && \
	echo ">>> 蓝绿自部署完成"
