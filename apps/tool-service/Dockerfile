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

# 复制项目配置文件
COPY apps/tool-service/pyproject.toml apps/tool-service/uv.lock apps/tool-service/uv.toml ./apps/tool-service/

# 同步依赖
RUN cd apps/tool-service && uv sync --index-url https://mirrors.aliyun.com/pypi/simple/ --extra-index-url https://mirrors.aliyun.com/pypi/simple/ --allow-insecure-host mirrors.aliyun.com

ARG GIT_SHA=unknown

# 复制应用代码
COPY apps/tool-service/app /app/apps/tool-service/app

# 设置时区为东八区
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app/apps/tool-service
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV GIT_SHA=${GIT_SHA}

RUN chown -R appuser:appuser /app

USER appuser

WORKDIR /app/apps/tool-service

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
