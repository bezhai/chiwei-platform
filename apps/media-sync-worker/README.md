# media-sync-worker

内部部署的媒体同步 worker，用于 Pixiv 图片下载任务和 Bangumi Archive 数据同步。

## 职责

- Pixiv 图片下载任务发现与消费
- Bangumi Archive 数据同步
- 通过飞书发送任务状态和失败通知

## 部署形态

本服务默认部署到内部 PaaS/K8s，作为 `port=0` worker 运行，不暴露 HTTP Service。

如果开启 `TAGGER_CALLBACK_SERVER_ENABLED=true`，本服务会同时启动一个内部 HTTP callback 入口，不再是纯 `port=0` worker。PaaS app 需要配置端口并创建 Service；tagger entry 的 callback URL 需要通过 gateway/internal reachable 地址打到该 Service。

数据仍连接云主机上的 MongoDB/Redis，不使用集群内的 `mongo` / `redis` ConfigBundle。切换到内部部署前，需要停掉云主机上的旧 worker，避免重复扫描和下载。

Tagger 结果存储例外：打标结果写入本地 Mongo，复用 channel-server 已在使用的本地 Mongo 基础设施，并通过 `TAGGER_RESULT_MONGO_*` 单独配置。不要把 tagger 结果写回旧 `img_map`。

Pixiv 图片元数据本地镜像例外：下载成功后，worker 会在 `PIXIV_IMAGE_MIRROR_MONGO_ENABLED=true` 时把旧 Mongo 中同 `pixiv_addr` 的源文档旁路 upsert 到本地 `chiwei_pixiv.pixiv_images`。这是下载链路的增量镜像，不改变旧 `MONGO_*` 的源库职责。

## 必需环境变量

- `MONGO_HOST`（可包含端口；包含端口时不再读取 `MONGO_PORT`）
- `MONGO_PORT`
- `MONGO_INITDB_ROOT_USERNAME`
- `MONGO_INITDB_ROOT_PASSWORD`
- `MONGO_CONNECT_TIMEOUT_MS`
- `REDIS_HOST`
- `REDIS_PORT`
- `REDIS_PASSWORD`
- `APP_ID`
- `APP_SECRET`
- `SELF_CHAT_ID`
- `HTTP_SECRET` 或 `PROXY_HTTP_SECRET`
- `BANGUMI_ACCESS_TOKEN`

## 可选环境变量

- `DOWNLOAD_CRON`：Pixiv 下载任务 cron 表达式，默认 `12 10 * * *`（每天 10:12）。
- `DOWNLOAD_AFTER_ILLUST_INFO_DELAY_MS`：取作品信息后的等待时间，默认 `1500`（旧值减半）。
- `DOWNLOAD_BEFORE_PAGE_DOWNLOAD_DELAY_MS`：单页图片代理下载前等待时间，默认 `1000`（旧值减半）。
- `DOWNLOAD_AFTER_TASK_DELAY_MS`：每个下载任务完成后的等待时间，默认 `2500`（旧值减半）。
- `DOWNLOAD_AFTER_AUTHOR_DELAY_MS`：作者发现阶段处理完一个作者后的等待时间，默认 `1500`（旧值减半）。
- `DOWNLOAD_LIMITER_COOLDOWN_MS`：每 60 个下载任务后的冷却时间，默认 `120000`（旧值减半）。
- `MINIO_SYNC_ENABLED`：开启 OSS→MinIO per-page 同步，默认关闭。
- `MINIO_SYNC_TIMEOUT_MS`：单张 OSS→MinIO 同步硬超时，默认 `30000`。
- `PIXIV_IMAGE_MIRROR_MONGO_ENABLED`：开启下载后本地 `pixiv_images` 镜像同步，默认关闭。
- `PIXIV_IMAGE_MIRROR_MONGO_*`：本地 Pixiv 图片元数据镜像 Mongo 配置，必须和旧 `MONGO_*` 分开。
- `TAGGER_RESULT_MONGO_ENABLED`：开启本地 Mongo 结果库连接，默认关闭。
- `TAGGER_RESULT_MONGO_*`：本地 tagger 结果 Mongo 配置，必须和旧 `MONGO_*` 分开。
- `TAGGER_TRIGGER_ENABLED`：开启下载后写入 tagger outbox，由后台 worker 自动提交 tagger，默认关闭。
- `TAGGER_ENTRY_URL` / `TAGGER_API_TOKEN`：tagger entry 调用地址和 caller token。
- `TAGGER_SUBMIT_BATCH_SIZE`：每个 tagger task 合并提交的图片数，默认 `1`；线上可设为 `4` 让 tagger-entry 对多图做一次 batch 推理。
- `TAGGER_TRIGGER_WORKER_IDLE_DELAY_MS` / `TAGGER_TRIGGER_RETRY_DELAY_MS` / `TAGGER_TRIGGER_PROCESSING_TIMEOUT_MS` / `TAGGER_TRIGGER_MAX_ATTEMPTS`：tagger outbox worker 轮询、重试和 processing 超时配置。
- `TAGGER_CALLBACK_URL` / `TAGGER_CALLBACK_AUTH_TOKEN`：tagger-service 回调地址和回调鉴权 token。
- `TAGGER_CALLBACK_SERVER_ENABLED` / `TAGGER_CALLBACK_PORT`：开启 worker 内部 HTTP callback 入口及端口。

## 部署验证开关

- `DISABLE_SCHEDULES=true`：不注册定时任务
- `DISABLE_CONSUMER=true`：不启动下载任务消费者

两个开关同时打开时，进程会保持存活，便于先验证镜像、Pod、环境变量和基础连接。

- `RUN_CONNECTIVITY_CHECK=true`：在两个开关同时打开时，只检查 MongoDB/Redis 连通性，不启动定时任务或消费者。

## 本地开发

```bash
bun install
bun run dev
bun run check
```

## 日志

```bash
make logs APP=media-sync-worker
```
