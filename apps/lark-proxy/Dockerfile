# Stage 1: Install dependencies
FROM oven/bun:1-alpine AS builder

WORKDIR /repo

# 先复制工作区元数据和 lockfile，利用 Docker layer cache
COPY package.json bun.lock ./
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY apps/lark-proxy/package.json ./apps/lark-proxy/package.json
COPY packages/ts-shared/package.json ./packages/ts-shared/package.json
COPY packages/pixiv-client/package.json ./packages/pixiv-client/package.json
COPY packages/lark-utils/package.json ./packages/lark-utils/package.json

RUN bun install --frozen-lockfile

# Stage 2: Production（保持 workspace 目录结构）
FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /repo

# 从 builder 复制 node_modules（保留 workspace 符号链接）
COPY --from=builder /repo/node_modules ./node_modules

# 复制源码
COPY apps/lark-proxy/tsconfig.json ./apps/lark-proxy/tsconfig.json
COPY apps/lark-proxy/src ./apps/lark-proxy/src

WORKDIR /repo/apps/lark-proxy

CMD ["bun", "src/index.ts"]
