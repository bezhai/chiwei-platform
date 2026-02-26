FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata coreutils
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

# Bun workspace 用 .bun store + symlink 安装依赖。
# Kaniko snapshot 丢弃 symlink → 需要展开为真实文件。
# 使用 GNU cp (coreutils) 的 -rL 确保正确跟随 symlink。
RUN bun install --frozen-lockfile \
    && echo "--- resolving workspace node_modules ---" \
    && for nm_dir in \
         apps/lark-server/node_modules \
         apps/lark-proxy/node_modules \
         packages/ts-shared/node_modules \
         packages/pixiv-client/node_modules \
         packages/lark-utils/node_modules; do \
         if [ -d "$nm_dir" ]; then \
           echo "  resolving $nm_dir" \
           && /usr/bin/cp -rL "$nm_dir" "${nm_dir}_real" \
           && rm -rf "$nm_dir" \
           && mv "${nm_dir}_real" "$nm_dir" \
           || echo "  WARN: failed to resolve $nm_dir"; \
         fi; \
       done \
    && rm -rf node_modules/.bun \
    && echo "--- rebuilding workspace links ---" \
    && mkdir -p apps/lark-server/node_modules/@inner \
    && rm -rf apps/lark-server/node_modules/@inner/shared \
              apps/lark-server/node_modules/@inner/pixiv-client \
              apps/lark-server/node_modules/@inner/lark-utils \
    && ln -sf ../../../packages/ts-shared apps/lark-server/node_modules/@inner/shared \
    && ln -sf ../../../packages/pixiv-client apps/lark-server/node_modules/@inner/pixiv-client \
    && ln -sf ../../../packages/lark-utils apps/lark-server/node_modules/@inner/lark-utils \
    && echo "=== Verify ===" \
    && ls apps/lark-server/node_modules/lodash/package.json \
    && ls apps/lark-server/node_modules/koa/package.json \
    && ls apps/lark-server/node_modules/uuid/package.json \
    && ls apps/lark-server/node_modules/axios/package.json \
    && ls apps/lark-server/node_modules/ioredis/package.json \
    && ls apps/lark-server/node_modules/@inner/shared/package.json \
    && echo "=== package count ===" \
    && ls apps/lark-server/node_modules/ | wc -l \
    && echo "=== All OK ==="

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
