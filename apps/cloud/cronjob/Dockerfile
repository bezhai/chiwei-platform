FROM oven/bun:1 AS builder

WORKDIR /repo

# 复制 workspace 结构 + lockfile
COPY package.json bun.lock ./
COPY apps/cloud/cronjob/package.json ./apps/cloud/cronjob/
COPY packages/ts-shared ./packages/ts-shared
COPY packages/lark-utils ./packages/lark-utils
COPY packages/pixiv-client ./packages/pixiv-client

# 占位其他 workspace 成员的 package.json（bun install 需要）
COPY apps/lark-server/package.json ./apps/lark-server/
COPY apps/lark-proxy/package.json ./apps/lark-proxy/
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/

RUN bun install --frozen-lockfile

# 复制源码
COPY apps/cloud/cronjob/tsconfig.json ./apps/cloud/cronjob/
COPY apps/cloud/cronjob/src ./apps/cloud/cronjob/src

# 编译为独立二进制
WORKDIR /repo/apps/cloud/cronjob
RUN bun build src/index.ts --compile --outfile=cronjob

# --- Runtime ---
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/cloud/cronjob/cronjob ./cronjob

CMD ["./cronjob"]
