FROM harbor.local:30002/library/bun:1.3.9-slim AS builder

WORKDIR /repo

# ① 仅复制 package.json + lockfile（变动频率低，保护 install 缓存层）
COPY package.json bun.lock bunfig.toml ./
COPY apps/channel-server/package.json ./apps/channel-server/
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/
COPY apps/media-sync-worker/package.json ./apps/media-sync-worker/
COPY apps/qq-gateway/package.json ./apps/qq-gateway/
COPY packages/ts-shared/package.json ./packages/ts-shared/
COPY packages/pixiv-client/package.json ./packages/pixiv-client/
COPY packages/lark-utils/package.json ./packages/lark-utils/

# ② 安装依赖（只有 package.json / lockfile / bunfig 变动才重新执行）
RUN bun install --frozen-lockfile

# ③ 复制 workspace 包源码 + channel-server 源码
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils
COPY apps/channel-server/tsconfig.json ./apps/channel-server/
COPY apps/channel-server/src ./apps/channel-server/src

# ④ 并行编译为独立二进制
WORKDIR /repo/apps/channel-server
RUN bun build src/index.ts --compile --outfile=server & \
    bun build src/workers/recall-worker.ts --compile --outfile=recall-worker & \
    bun build src/workers/chat-response-worker.ts --compile --outfile=chat-response-worker & \
    wait && \
    test -f server && test -f recall-worker && test -f chat-response-worker

# --- Runtime（同样基于 Debian/glibc，预装 ca-certificates/tzdata）---
FROM harbor.local:30002/inner-bot/lark-server-runtime@sha256:e25a0e85ea61dba92b3df8acaff2e1c532bd2235463302d827ed6f1a4d6101cc

ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/channel-server/server ./server
COPY --from=builder /repo/apps/channel-server/recall-worker ./recall-worker
COPY --from=builder /repo/apps/channel-server/chat-response-worker ./chat-response-worker

RUN mkdir -p /var/log/channel-server && chmod 777 /var/log/channel-server

CMD ["./server"]
