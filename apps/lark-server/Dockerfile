FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata nodejs npm
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /repo

# 复制所有 workspace 元数据和共享包源码
COPY package.json bun.lock ./
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY apps/lark-proxy/package.json ./apps/lark-proxy/package.json
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# 用 npm 安装依赖（而非 bun install）。
# npm 创建扁平化的 node_modules，无 symlink store，
# 完美兼容 Kaniko 的 snapshot 机制。运行时仍用 Bun。
RUN npm install --include=dev 2>&1 | tail -5 \
    && echo "=== Verify ===" \
    && ls apps/lark-server/node_modules/lodash/package.json 2>/dev/null \
       || ls node_modules/lodash/package.json \
    && echo "=== package count ===" \
    && ls node_modules/ | wc -l \
    && ls apps/lark-server/node_modules/ 2>/dev/null | wc -l \
    && echo "=== All OK ==="

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
