FROM oven/bun:1

ENV TZ=Asia/Shanghai

WORKDIR /app

# 创建包含 cronjob 和所有依赖包的 workspace 配置
RUN echo '{"name":"cronjob-build","private":true,"workspaces":["apps/cloud/cronjob","packages/ts-shared","packages/lark-utils","packages/pixiv-client"]}' > package.json

# 复制所有依赖包
COPY packages/ts-shared ./packages/ts-shared
COPY packages/lark-utils ./packages/lark-utils
COPY packages/pixiv-client ./packages/pixiv-client

# 复制 cronjob 的 package.json
COPY apps/cloud/cronjob/package.json ./apps/cloud/cronjob/

# 安装依赖
RUN bun install

# 复制 cronjob 源代码
COPY apps/cloud/cronjob/src ./apps/cloud/cronjob/src
COPY apps/cloud/cronjob/tsconfig.json ./apps/cloud/cronjob/

ENV NODE_ENV=production

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app/apps/cloud/cronjob

CMD ["bun", "run", "src/index.ts"]
