FROM oven/bun:1 AS builder

WORKDIR /repo

# 最小 workspace：只包含 cronjob + 它依赖的 @inner/* 包
RUN echo '{"private":true,"workspaces":["apps/cloud/cronjob","packages/ts-shared","packages/lark-utils","packages/pixiv-client"]}' > package.json

COPY apps/cloud/cronjob/package.json ./apps/cloud/cronjob/
COPY packages/ts-shared ./packages/ts-shared
COPY packages/lark-utils ./packages/lark-utils
COPY packages/pixiv-client ./packages/pixiv-client

RUN bun install

# 复制源码
COPY apps/cloud/cronjob/tsconfig.json ./apps/cloud/cronjob/
COPY apps/cloud/cronjob/src ./apps/cloud/cronjob/src

# 编译为独立二进制
WORKDIR /repo/apps/cloud/cronjob
RUN bun build src/index.ts --compile --outfile=cronjob --external @aws-sdk/client-s3

# --- Runtime ---
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app
COPY --from=builder /repo/apps/cloud/cronjob/cronjob ./cronjob

CMD ["./cronjob"]
