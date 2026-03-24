FROM harbor.local:30002/library/python:3.11-slim

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends tzdata curl bash git && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r appuser && useradd -r -g appuser -m appuser

WORKDIR /app

RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set global.trusted-host mirrors.aliyun.com

RUN pip install uv

# 复制项目配置并安装依赖
COPY apps/sandbox-worker/pyproject.toml apps/sandbox-worker/uv.toml ./apps/sandbox-worker/
RUN cd apps/sandbox-worker && uv sync --index-url https://mirrors.aliyun.com/pypi/simple/ --extra-index-url https://mirrors.aliyun.com/pypi/simple/ --allow-insecure-host mirrors.aliyun.com

# 复制 skill 定义（sandbox 需要访问脚本文件）
COPY apps/agent-service/app/skills/definitions /sandbox/skills/

ARG GIT_SHA=unknown

# 复制应用代码
COPY apps/sandbox-worker/app /app/apps/sandbox-worker/app

ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app/apps/sandbox-worker
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV GIT_SHA=${GIT_SHA}

RUN chown -R appuser:appuser /app /sandbox

USER appuser

WORKDIR /app/apps/sandbox-worker

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
