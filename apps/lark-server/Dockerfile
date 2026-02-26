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

# Bun workspace 的安装结构:
#   - node_modules/.bun/ 是真实文件 store
#   - 每个 workspace 的 node_modules/ 内都是指向 .bun store 的 symlink
# Kaniko snapshot 会丢弃 symlink，所以需要把每个 workspace 的
# node_modules 用 cp -rL 展开为真实文件，再删掉 .bun store。
RUN bun install --frozen-lockfile \
    && for nm_dir in apps/lark-server/node_modules apps/lark-proxy/node_modules packages/ts-shared/node_modules packages/pixiv-client/node_modules packages/lark-utils/node_modules; do \
         if [ -d "$nm_dir" ]; then \
           cp -rL "$nm_dir" "${nm_dir}_real" \
           && rm -rf "$nm_dir" \
           && mv "${nm_dir}_real" "$nm_dir"; \
         fi; \
       done \
    && rm -rf node_modules/.bun \
    && mkdir -p apps/lark-server/node_modules/@inner \
    && ln -sf ../../../packages/ts-shared apps/lark-server/node_modules/@inner/shared \
    && ln -sf ../../../packages/pixiv-client apps/lark-server/node_modules/@inner/pixiv-client \
    && ln -sf ../../../packages/lark-utils apps/lark-server/node_modules/@inner/lark-utils \
    && echo "=== Verify ===" \
    && ls apps/lark-server/node_modules/lodash/package.json \
    && ls apps/lark-server/node_modules/koa/package.json \
    && ls apps/lark-server/node_modules/uuid/package.json \
    && ls apps/lark-server/node_modules/@inner/shared/package.json \
    && echo "=== All OK ==="

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
