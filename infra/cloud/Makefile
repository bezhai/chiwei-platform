# 云服务器基础设施部署

.PHONY: up down restart logs ps init

# 启动服务
up:
	docker compose up -d

# 停止服务
down:
	docker compose down

# 重启服务
restart:
	docker compose restart

# 查看日志
logs:
	docker compose logs -f

# 查看服务状态
ps:
	docker compose ps

# 初始化环境
init:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "已创建 .env，请编辑填写实际配置"; \
	else \
		echo ".env 已存在"; \
	fi
