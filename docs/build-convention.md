# 构建约定与服务接入指南

## 构建上下文模式

PaaS Engine 通过 Kaniko 在 K8s 中构建 Docker 镜像。构建时需要指定 **构建上下文**（Kaniko 可以访问的文件范围）和 **Dockerfile 位置**。根据服务是否依赖共享包，有两种模式：

### 模式一：独立构建（默认）

适用于不依赖 `packages/` 共享包、也不在 Bun/pnpm workspace 中的服务。

- `context_dir` 设为服务目录，如 `apps/my-standalone-service`
- Kaniko 使用 `--context-sub-path` 将上下文限定到该子目录
- Dockerfile 必须在该子目录根下

```
POST /api/v1/apps/my-standalone-service/builds/
{
  "git_ref": "main",
  "context_dir": "apps/my-standalone-service"
}
```

### 模式二：Monorepo 根目录构建

适用于 Bun workspace 中的 TS 服务（如 lark-server、lark-proxy）或依赖 `packages/` 共享包的服务。所有 TS 服务统一使用此模式。

- `context_dir` 设为 `.`（repo 根目录）
- Kaniko 不使用 `--context-sub-path`，上下文为整个仓库
- **Dockerfile 路径自动推导**：`apps/<app_name>/Dockerfile`（通过 `--dockerfile` 参数指定）

```
POST /api/v1/apps/lark-server/builds/
{
  "git_ref": "main",
  "context_dir": "."
}
```

### 推导规则总结

| `context_dir` | Kaniko 行为 | Dockerfile 位置 |
|---|---|---|
| 空（不传） | 上下文 = repo 根，默认 Dockerfile | `./Dockerfile`（repo 根） |
| `.` | 上下文 = repo 根，推导 Dockerfile | `apps/<app_name>/Dockerfile` |
| `apps/xxx` | 上下文 = `apps/xxx` 子目录 | `apps/xxx/Dockerfile` |

## 新服务接入步骤

### 1. 创建目录结构

```
apps/
  my-service/
    Dockerfile
    ...（服务代码）
```

如果服务依赖共享包：

```
apps/
  my-service/
    Dockerfile        # COPY 时使用相对 repo 根的路径
packages/
  shared-lib/         # 共享包
```

### 2. 编写 Dockerfile

#### TypeScript / Bun Workspace

所有 TS 服务使用 Bun workspace，构建时需 `context_dir=.`。

```dockerfile
FROM oven/bun:1-alpine AS builder
WORKDIR /repo

# 复制工作区元数据和 lockfile（必须包含所有 workspace 成员的 package.json）
COPY package.json bun.lock ./
COPY apps/my-service/package.json ./apps/my-service/package.json
COPY apps/other-service/package.json ./apps/other-service/package.json
COPY packages/ts-shared/package.json ./packages/ts-shared/package.json
# ... 列出根 package.json workspaces 中声明的所有成员

RUN bun install --frozen-lockfile

# 复制本服务源码及依赖的共享包
COPY apps/my-service ./apps/my-service
COPY packages/ts-shared ./packages/ts-shared

# 构建共享包（如果有 build 步骤）
RUN cd packages/ts-shared && bun run build

# 构建主服务
RUN cd apps/my-service && bun build src/index.ts --target=bun --outdir=dist --packages external

RUN bun install --production

FROM oven/bun:1-alpine
WORKDIR /usr/src/app
COPY --from=builder /repo/apps/my-service/dist ./dist
COPY --from=builder /repo/node_modules ./node_modules
COPY --from=builder /repo/apps/my-service/package.json ./package.json
CMD ["bun", "dist/index.js"]
```

> **注意**：
> - 使用根目录构建，COPY 路径相对于 repo 根目录
> - `--packages external` 避免 bundler 尝试解析 workspace 依赖
> - 所有 workspace 成员的 `package.json` 都必须 COPY，否则 `bun install` 会报 workspace not found
> - workspace 内部依赖使用 `workspace:*` 协议，不要用 `file:` 协议

#### Python（依赖共享包）

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY apps/my-service/pyproject.toml apps/my-service/
COPY packages/py-shared/ packages/py-shared/
RUN pip install --no-cache-dir -e packages/py-shared
RUN pip install --no-cache-dir apps/my-service/

COPY apps/my-service/ apps/my-service/

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /app/apps/my-service ./
CMD ["python", "-m", "my_service"]
```

#### Go（独立构建）

```dockerfile
FROM golang:1.25-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bin/my-service ./cmd/my-service

FROM alpine:3.19
COPY --from=builder /bin/my-service /bin/my-service
CMD ["/bin/my-service"]
```

> Go 服务通常不依赖 monorepo 共享包，使用独立构建模式（`context_dir=apps/my-service`）即可。

### 3. 注册应用

```bash
curl -X POST https://paas-engine/api/v1/apps/ \
  -H "X-API-Key: $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-service",
    "git_repo": "https://github.com/org/chiwei_platform_lark",
    "image_repo": "registry.example.com/my-service"
  }'
```

### 4. 提交并推送代码

Kaniko 构建时通过 `git://` 协议从**远程仓库**克隆代码，不会读取本地未推送的改动。触发构建前必须确保代码已推送到远程：

```bash
git add .
git commit -m "feat(my-service): add service"
git push origin <branch>
```

### 5. 触发构建

独立构建：

```bash
curl -X POST https://paas-engine/api/v1/apps/my-service/builds/ \
  -H "X-API-Key: $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"git_ref": "main", "context_dir": "apps/my-service"}'
```

依赖共享包的根目录构建：

```bash
curl -X POST https://paas-engine/api/v1/apps/my-service/builds/ \
  -H "X-API-Key: $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"git_ref": "main", "context_dir": "."}'
```

### 6. 发布

```bash
# 发布到 prod 泳道
curl -X POST https://paas-engine/api/v1/releases/ \
  -H "X-API-Key: $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"app_name": "my-service", "lane": "prod", "image_tag": "registry.example.com/my-service:<tag>"}'
```

或使用 Makefile：

```bash
make deploy APP=my-service
```

## 共享包（packages/）

`packages/` 目录存放跨服务共享的代码包：

| 包 | 语言 | 说明 |
|---|---|---|
| `ts-shared` | TypeScript | TypeScript 服务共享工具 |
| `py-shared` | Python | Python 服务共享工具 |
| `lark-utils` | TypeScript | 飞书/Lark SDK 封装 |
| `pixiv-client` | TypeScript | Pixiv API 客户端 |

使用共享包的服务必须：
1. 将 `context_dir` 设为 `.`（根目录构建）
2. 在 Dockerfile 中 COPY 对应的 `packages/` 目录
3. Dockerfile 放在 `apps/<service>/Dockerfile`
