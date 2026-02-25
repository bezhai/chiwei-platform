FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /repo

# 先复制工作区元数据和 lockfile，利用 Docker layer cache
COPY package.json bun.lock ./
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY apps/lark-proxy/package.json ./apps/lark-proxy/package.json
COPY packages/ts-shared/package.json ./packages/ts-shared/package.json
COPY packages/pixiv-client/package.json ./packages/pixiv-client/package.json
COPY packages/lark-utils/package.json ./packages/lark-utils/package.json

RUN bun install --frozen-lockfile \
    && echo "=== DEBUG: node_modules/@inner ===" \
    && ls -la node_modules/@inner/ \
    && echo "=== DEBUG: feishu-card ===" \
    && ls node_modules/feishu-card/package.json \
    && echo "=== DEBUG: symlink targets ===" \
    && readlink node_modules/@inner/shared || true

# 复制并构建共享包
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

RUN cd packages/ts-shared && bun run build
RUN cd packages/pixiv-client && bun run build
RUN cd packages/lark-utils && bun run build

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
