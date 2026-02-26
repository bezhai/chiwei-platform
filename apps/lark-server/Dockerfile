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

# 安装依赖，然后用 tar -h 解析所有 node_modules 中的 symlink
# Bun workspace 会将一些包安装到子目录 node_modules（如 apps/lark-server/node_modules），
# 必须对所有 node_modules 目录都做 symlink 解析。
RUN bun install --frozen-lockfile \
    && for nm in $(find /repo -name node_modules -not -path '*/node_modules/*'); do \
         parent=$(dirname "$nm"); \
         cd "$parent" \
         && tar -chf /tmp/_nm.tar node_modules \
         && rm -rf node_modules \
         && tar -xf /tmp/_nm.tar \
         && rm /tmp/_nm.tar; \
       done \
    && cd /repo \
    && find /repo -type d -name .bun -exec rm -rf {} + 2>/dev/null; true \
    && mkdir -p node_modules/@inner \
    && rm -rf node_modules/@inner/shared node_modules/@inner/pixiv-client node_modules/@inner/lark-utils \
    && ln -sf ../../packages/ts-shared node_modules/@inner/shared \
    && ln -sf ../../packages/pixiv-client node_modules/@inner/pixiv-client \
    && ln -sf ../../packages/lark-utils node_modules/@inner/lark-utils \
    && echo "=== Verify root ===" && ls node_modules/koa/package.json \
    && echo "=== Verify workspace ===" && ls apps/lark-server/node_modules/lodash/package.json

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
