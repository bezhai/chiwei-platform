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

# ③ 复制 workspace 包源码 + 本服务源码。
# 这两个包是 source-only（package.json 的 main 指向 src/*.ts），install 阶段只建了
# 符号链接、没有把源码拷进来，漏一个的症状是 bun build 报模块找不到。
COPY packages/ts-shared ./packages/ts-shared
COPY packages/lark-utils ./packages/lark-utils
COPY apps/lark-service/tsconfig.json ./apps/lark-service/
COPY apps/lark-service/src ./apps/lark-service/src

# ④ 并行编译为独立二进制。
# 一个镜像两个 Deployment：入口进程持飞书 WS 长连（单副本 + Recreate），出站是竞争
# 消费（可多副本、可滚动更新）。两种部署策略天然冲突，所以它们是两个进程。
WORKDIR /repo/apps/lark-service
RUN bun build src/index.ts --compile --outfile=server & \
    bun build src/outbound.ts --compile --outfile=lark-outbound & \
    wait && \
    test -f server && test -f lark-outbound

# --- Runtime（同样基于 Debian/glibc，预装 ca-certificates/tzdata）---
FROM harbor.local:30002/inner-bot/lark-server-runtime:bookworm-slim

ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/lark-service/server ./server
COPY --from=builder /repo/apps/lark-service/lark-outbound ./lark-outbound

# 默认跑入口进程。出站那个 Deployment 由 PaaS 覆盖 command 成 ./lark-outbound
# —— 同一个镜像、两个进程，跟 channel-server 那三个是同一套做法。
CMD ["./server"]
