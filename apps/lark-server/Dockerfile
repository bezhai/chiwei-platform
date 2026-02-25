# Stage 1: Build dependencies & shared packages
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

COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# 构建共享包
RUN cd packages/ts-shared && bun run build
RUN cd packages/pixiv-client && bun run build
RUN cd packages/lark-utils && bun run build

RUN bun install --production

# Stage 2: Production（直接运行 TS 源码，bun 原生支持）
FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /usr/src/app

COPY --from=builder /repo/node_modules ./node_modules
COPY --from=builder /repo/packages/ts-shared ./packages/ts-shared
COPY --from=builder /repo/packages/pixiv-client ./packages/pixiv-client
COPY --from=builder /repo/packages/lark-utils ./packages/lark-utils
COPY apps/lark-server/package.json ./package.json
COPY apps/lark-server/tsconfig.json ./tsconfig.json
COPY apps/lark-server/src ./src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

CMD ["bun", "src/index.ts"]
