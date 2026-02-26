###############################################
# Stage 1: 安装依赖并将 symlink 打包为真实文件
###############################################
FROM oven/bun:1-alpine AS deps

WORKDIR /repo

COPY package.json bun.lock ./
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY apps/lark-proxy/package.json ./apps/lark-proxy/package.json
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# 安装依赖后用 tar -h 解析所有 symlink 并打包
RUN bun install --frozen-lockfile \
    && tar -chf /tmp/node_modules.tar node_modules \
    && rm -rf node_modules \
    && tar -xf /tmp/node_modules.tar \
    && rm /tmp/node_modules.tar \
    && rm -rf node_modules/.bun \
    && rm -rf apps/lark-server/node_modules \
    && rm -rf apps/lark-proxy/node_modules \
    && rm -rf packages/*/node_modules \
    && mkdir -p node_modules/@inner \
    && rm -rf node_modules/@inner/shared node_modules/@inner/pixiv-client node_modules/@inner/lark-utils \
    && ln -sf ../../packages/ts-shared node_modules/@inner/shared \
    && ln -sf ../../packages/pixiv-client node_modules/@inner/pixiv-client \
    && ln -sf ../../packages/lark-utils node_modules/@inner/lark-utils \
    && echo "=== Verify: lodash ===" && ls node_modules/lodash/package.json \
    && echo "=== Verify: uuid ===" && ls node_modules/uuid/package.json

###############################################
# Stage 2: 运行镜像
###############################################
FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /repo

# 从 deps stage 复制已解析的 node_modules 和 packages
COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/packages ./packages

# 复制源码
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

# 验证关键包存在
RUN echo "=== Stage 2 Verify ===" \
    && ls node_modules/lodash/package.json \
    && ls node_modules/uuid/package.json \
    && ls node_modules/@inner/shared/package.json \
    && echo "=== All OK ==="

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
