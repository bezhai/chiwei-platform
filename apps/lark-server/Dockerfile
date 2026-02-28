FROM oven/bun:1.3.9-slim AS builder

WORKDIR /repo

# 复制 workspace 结构 + lockfile
COPY package.json bun.lock ./
COPY apps/lark-server/package.json ./apps/lark-server/
COPY apps/lark-proxy/package.json ./apps/lark-proxy/
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# bun install（symlink 仅用于编译阶段，不影响最终产物）
RUN bun install --frozen-lockfile

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/
COPY apps/lark-server/src ./apps/lark-server/src

# 编译为独立二进制
WORKDIR /repo/apps/lark-server
RUN bun build src/index.ts --compile --outfile=server
RUN bun build src/workers/recall-worker.ts --compile --outfile=recall-worker

# --- Runtime（同样基于 Debian/glibc）---
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/lark-server/server ./server
COPY --from=builder /repo/apps/lark-server/recall-worker ./recall-worker

RUN mkdir -p /var/log/lark-server && chmod 777 /var/log/lark-server

CMD ["./server"]
