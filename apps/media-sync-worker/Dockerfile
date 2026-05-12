FROM harbor.local:30002/library/bun:1.3.9-slim AS builder

WORKDIR /repo

# 复制 workspace 元数据和 lockfile，匹配 PaaS / Kaniko 的根目录构建模式
COPY package.json bun.lock bunfig.toml ./
COPY apps/lark-server/package.json ./apps/lark-server/
COPY apps/lark-proxy/package.json ./apps/lark-proxy/
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/
COPY apps/media-sync-worker/package.json ./apps/media-sync-worker/
COPY packages/ts-shared/package.json ./packages/ts-shared/
COPY packages/pixiv-client/package.json ./packages/pixiv-client/
COPY packages/lark-utils/package.json ./packages/lark-utils/

RUN bun install --frozen-lockfile

COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# 复制源码
COPY apps/media-sync-worker/tsconfig.json ./apps/media-sync-worker/
COPY apps/media-sync-worker/src ./apps/media-sync-worker/src

# 编译为独立二进制
WORKDIR /repo/apps/media-sync-worker
RUN bun build src/index.ts --compile --outfile=media-sync-worker --external @aws-sdk/client-s3

# --- Runtime（复用内部 glibc 运行时，避免构建时访问外部 apt 源）---
FROM harbor.local:30002/inner-bot/lark-server-runtime@sha256:e25a0e85ea61dba92b3df8acaff2e1c532bd2235463302d827ed6f1a4d6101cc

ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/media-sync-worker/media-sync-worker ./media-sync-worker

CMD ["./media-sync-worker"]
