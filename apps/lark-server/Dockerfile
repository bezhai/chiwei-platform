FROM harbor.local:30002/library/bun:1.3.9-slim AS builder

WORKDIR /repo

# ① 仅复制 package.json + lockfile（变动频率低，保护 install 缓存层）
COPY package.json bun.lock bunfig.toml ./
COPY apps/lark-server/package.json ./apps/lark-server/
COPY apps/lark-proxy/package.json ./apps/lark-proxy/
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/
COPY packages/ts-shared/package.json ./packages/ts-shared/
COPY packages/pixiv-client/package.json ./packages/pixiv-client/
COPY packages/lark-utils/package.json ./packages/lark-utils/

# ② 安装依赖（只有 package.json / lockfile / bunfig 变动才重新执行）
RUN bun install --frozen-lockfile

# ③ 复制 workspace 包源码 + lark-server 源码
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils
COPY apps/lark-server/tsconfig.json ./apps/lark-server/
COPY apps/lark-server/src ./apps/lark-server/src

# ④ 并行编译为独立二进制
WORKDIR /repo/apps/lark-server
RUN bun build src/index.ts --compile --outfile=server & \
    bun build src/workers/recall-worker.ts --compile --outfile=recall-worker & \
    bun build src/workers/chat-response-worker.ts --compile --outfile=chat-response-worker & \
    wait && \
    test -f server && test -f recall-worker && test -f chat-response-worker

# --- Runtime（同样基于 Debian/glibc）---
FROM harbor.local:30002/library/debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/lark-server/server ./server
COPY --from=builder /repo/apps/lark-server/recall-worker ./recall-worker
COPY --from=builder /repo/apps/lark-server/chat-response-worker ./chat-response-worker

RUN mkdir -p /var/log/lark-server && chmod 777 /var/log/lark-server

CMD ["./server"]
