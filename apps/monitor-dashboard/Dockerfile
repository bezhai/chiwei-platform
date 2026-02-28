FROM oven/bun:1.3.9-alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /repo

# Copy all workspace metadata and shared packages
COPY package.json bun.lock ./
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY apps/lark-proxy/package.json ./apps/lark-proxy/package.json
COPY apps/monitor-dashboard/package.json ./apps/monitor-dashboard/package.json
COPY apps/monitor-dashboard-web/package.json ./apps/monitor-dashboard-web/package.json
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# Install dependencies with copyfile backend (Kaniko-friendly)
RUN bun install --frozen-lockfile --backend=copyfile \
    && mkdir -p node_modules/@inner \
    && rm -rf node_modules/@inner/shared \
    && cp -rL packages/ts-shared node_modules/@inner/shared

# Copy app source
COPY apps/monitor-dashboard/tsconfig.json ./apps/monitor-dashboard/tsconfig.json
COPY apps/monitor-dashboard/src ./apps/monitor-dashboard/src

WORKDIR /repo/apps/monitor-dashboard

CMD ["bun", "src/index.ts"]
