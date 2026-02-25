# Stage 1: Build
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

COPY apps/lark-proxy ./apps/lark-proxy

RUN cd apps/lark-proxy && bun build src/index.ts --target=bun --outdir=dist --packages external

RUN bun install --production

# Stage 2: Production
FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /usr/src/app

COPY --from=builder /repo/apps/lark-proxy/dist ./dist
COPY --from=builder /repo/node_modules ./node_modules
COPY --from=builder /repo/apps/lark-proxy/package.json ./package.json

CMD ["bun", "dist/index.js"]
