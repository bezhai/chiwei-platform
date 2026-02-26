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

# 用 npm 安装（无 symlink store，完美兼容 Kaniko）。
# 1. 将 bun 的 "workspace:*" 替换为 npm 兼容的 "*"
# 2. npm install 创建扁平 node_modules，workspace 包是 symlink
# 3. 将 workspace 的 3 个 symlink 替换为真实拷贝
RUN sed -i 's/"workspace:\*"/"*"/g' apps/lark-server/package.json \
    && npm install --include=dev 2>&1 | tail -3 \
    && echo "--- replacing workspace symlinks with real copies ---" \
    && for pkg in node_modules/@inner/shared node_modules/@inner/pixiv-client node_modules/@inner/lark-utils; do \
         if [ -L "$pkg" ]; then \
           target=$(readlink -f "$pkg"); \
           rm "$pkg"; \
           cp -r "$target" "$pkg"; \
           echo "  resolved $pkg"; \
         fi; \
       done \
    && echo "=== Verify ===" \
    && ls node_modules/lodash/package.json \
    && ls node_modules/koa/package.json \
    && ls node_modules/uuid/package.json \
    && ls node_modules/axios/package.json \
    && ls node_modules/@inner/shared/package.json \
    && echo "=== package count: $(ls node_modules/ | wc -l) ===" \
    && echo "=== All OK ==="

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
