FROM harbor.local:30002/library/bun:1.3.9-slim AS builder

WORKDIR /repo

# ① 仅复制 package.json + lockfile（变动频率低，保护 install 缓存层）
# bun workspaces 的 install 要求**所有** workspace 成员的 package.json 都在，
# 少一个直接 "Workspace not found" 而不是跳过。
COPY package.json bun.lock bunfig.toml ./
COPY apps/channel-server/package.json ./apps/channel-server/
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/
COPY apps/media-sync-worker/package.json ./apps/media-sync-worker/
COPY apps/qq-gateway/package.json ./apps/qq-gateway/
COPY apps/lark-service/package.json ./apps/lark-service/
COPY packages/ts-shared/package.json ./packages/ts-shared/
COPY packages/pixiv-client/package.json ./packages/pixiv-client/
COPY packages/lark-utils/package.json ./packages/lark-utils/

# ② 安装依赖（只有 package.json / lockfile / bunfig 变动才重新执行）
RUN bun install --frozen-lockfile

# ③ 复制 workspace 包源码（lark-service 目前只依赖 @inner/shared）+ 本服务源码
COPY packages/ts-shared ./packages/ts-shared
COPY apps/lark-service/tsconfig.json ./apps/lark-service/
COPY apps/lark-service/src ./apps/lark-service/src

# ④ 编译为独立二进制
WORKDIR /repo/apps/lark-service
RUN bun build src/index.ts --compile --outfile=server && test -f server

# --- Runtime（同样基于 Debian/glibc，预装 ca-certificates/tzdata）---
FROM harbor.local:30002/inner-bot/lark-server-runtime:bookworm-slim

ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/lark-service/server ./server

CMD ["./server"]
