FROM oven/bun:1.3.9-alpine

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

# 安装依赖：kaniko snapshot 会丢弃 bun 的 symlink store，
# 用 --backend=copyfile 让 bun 直接产生真实文件而非 symlink。
RUN bun install --frozen-lockfile --backend=copyfile \
    && mkdir -p node_modules/@inner \
    && rm -rf node_modules/@inner/shared node_modules/@inner/pixiv-client node_modules/@inner/lark-utils \
    && cp -rL packages/ts-shared node_modules/@inner/shared \
    && cp -rL packages/pixiv-client node_modules/@inner/pixiv-client \
    && cp -rL packages/lark-utils node_modules/@inner/lark-utils

# 复制源码
COPY apps/lark-proxy/tsconfig.json ./apps/lark-proxy/tsconfig.json
COPY apps/lark-proxy/src ./apps/lark-proxy/src

WORKDIR /repo/apps/lark-proxy

CMD ["bun", "src/index.ts"]
