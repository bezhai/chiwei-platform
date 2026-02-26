FROM python:3.11-slim

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends tzdata curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r appuser && useradd -r -g appuser -m appuser

WORKDIR /app

# 配置pip使用阿里云镜像源
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set global.trusted-host mirrors.aliyun.com

# 安装uv
RUN pip install uv

# 复制项目配置文件（monorepo：apps + packages）
COPY apps/agent-service/pyproject.toml apps/agent-service/uv.lock apps/agent-service/uv.toml ./apps/agent-service/
COPY packages/py-shared ./packages/py-shared

# 配置uv使用阿里云镜像源并同步依赖
RUN cd apps/agent-service && uv sync --index-url https://mirrors.aliyun.com/pypi/simple/ --extra-index-url https://mirrors.aliyun.com/pypi/simple/ --allow-insecure-host mirrors.aliyun.com

# GIT_SHA 放在 COPY 源码之前：改了 commit 就会使源码层缓存失效，
# 而上方的依赖安装（uv sync）仍然走缓存。
ARG GIT_SHA=unknown

# 复制应用代码
COPY apps/agent-service/app /app/apps/agent-service/app

# 设置时区为东八区
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app/apps/agent-service
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV GIT_SHA=${GIT_SHA}

RUN mkdir -p /logs/agent-service && chown -R appuser:appuser /logs

RUN chown -R appuser:appuser /app

USER appuser

# 切换到项目目录，让 uv run 能找到虚拟环境
WORKDIR /app/apps/agent-service

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "app/config/logging_config.json"]
