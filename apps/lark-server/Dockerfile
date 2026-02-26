FROM oven/bun:1-alpine

RUN apk add --no-cache tzdata nodejs npm
ENV TZ=Asia/Shanghai

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /repo

# 只复制 lark-server 的 package.json 和 workspace 包源码
COPY apps/lark-server/package.json ./apps/lark-server/package.json
COPY packages/ts-shared ./packages/ts-shared
COPY packages/pixiv-client ./packages/pixiv-client
COPY packages/lark-utils ./packages/lark-utils

# 不使用 workspace，直接在 lark-server 目录下安装所有依赖。
# 1. 将 workspace:* 替换为 file: 相对路径引用
# 2. 用 npm install 在 apps/lark-server 下创建扁平 node_modules
# 3. 所有包（含传递依赖）都在同一个 node_modules 下，无 hoisting 问题
RUN cd apps/lark-server \
    && sed -i \
         -e 's|"@inner/shared": "workspace:\*"|"@inner/shared": "file:../../packages/ts-shared"|' \
         -e 's|"@inner/pixiv-client": "workspace:\*"|"@inner/pixiv-client": "file:../../packages/pixiv-client"|' \
         package.json \
    && npm install --include=dev 2>&1 | tail -5 \
    && echo "--- resolve file: symlinks (kaniko strips symlinks) ---" \
    && for pkg in node_modules/@inner/shared node_modules/@inner/pixiv-client; do \
         if [ -L "$pkg" ]; then \
           target=$(readlink -f "$pkg"); \
           rm "$pkg"; \
           cp -r "$target" "$pkg"; \
           rm -rf "$pkg/node_modules"; \
           echo "  resolved $pkg -> $target"; \
         fi; \
       done \
    && echo "=== Verify ===" \
    && ls node_modules/lodash/package.json \
    && ls node_modules/koa/package.json \
    && ls node_modules/uuid/package.json \
    && ls node_modules/vary/package.json \
    && ls node_modules/@inner/shared/package.json \
    && echo "=== package count: $(ls node_modules/ | wc -l) ===" \
    && echo "=== All OK ==="

# 复制源码
COPY apps/lark-server/tsconfig.json ./apps/lark-server/tsconfig.json
COPY apps/lark-server/src ./apps/lark-server/src

RUN mkdir -p /var/log/lark-server && \
    chmod 777 /var/log/lark-server

WORKDIR /repo/apps/lark-server

CMD ["sh", "-c", "echo '=== @inner/shared contents ===' && ls -la node_modules/@inner/shared/ && echo '=== package.json ===' && cat node_modules/@inner/shared/package.json | head -8 && echo '=== src dir? ===' && ls node_modules/@inner/shared/src/ 2>&1 || echo 'NO src dir' && echo '=== Starting ===' && exec bun src/index.ts"]
