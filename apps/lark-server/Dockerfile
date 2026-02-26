FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata
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

# 调试：看看 bun install 之后的目录结构
RUN bun install --frozen-lockfile \
    && echo "=== ROOT node_modules (top 30) ===" \
    && ls node_modules/ | head -30 \
    && echo "=== lodash? ===" \
    && ls -la node_modules/lodash 2>/dev/null || echo "NO lodash in root nm" \
    && echo "=== koa? ===" \
    && ls -la node_modules/koa 2>/dev/null || echo "NO koa in root nm" \
    && echo "=== .bun dir? ===" \
    && ls node_modules/.bun/ 2>/dev/null | head -5 || echo "NO .bun dir" \
    && echo "=== workspace nm dirs ===" \
    && find /repo -name node_modules -not -path '*/node_modules/*' -type d \
    && echo "=== apps/lark-server/node_modules? ===" \
    && ls apps/lark-server/node_modules/ 2>/dev/null | head -20 || echo "NO apps/lark-server/nm" \
    && echo "=== apps/lark-server/nm/lodash? ===" \
    && ls -la apps/lark-server/node_modules/lodash 2>/dev/null || echo "NO lodash in app nm" \
    && echo "=== type of root nm/koa ===" \
    && file node_modules/koa 2>/dev/null || echo "cannot file koa" \
    && echo "=== readlink koa ===" \
    && readlink -f node_modules/koa 2>/dev/null || echo "cannot readlink koa" \
    && echo "=== DEBUG DONE ==="
