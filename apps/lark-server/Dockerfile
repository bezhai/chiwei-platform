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

# 安装依赖：kaniko snapshot 会丢弃 bun 的 symlink store，
# 用 cp -rL 将所有 symlink 展开为真实文件，再重建 workspace 链接。
RUN bun install --frozen-lockfile \
    && cp -rL node_modules node_modules_real \
    && rm -rf node_modules \
    && mv node_modules_real node_modules \
    && rm -rf node_modules/.bun \
    && mkdir -p node_modules/@inner \
    && rm -rf node_modules/@inner/shared node_modules/@inner/pixiv-client node_modules/@inner/lark-utils \
    && ln -sf ../../packages/ts-shared node_modules/@inner/shared \
    && ln -sf ../../packages/pixiv-client node_modules/@inner/pixiv-client \
    && ln -sf ../../packages/lark-utils node_modules/@inner/lark-utils

# Bun 直接运行 TypeScript，无需构建共享包

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["bun", "src/index.ts"]
