# media-sync-worker

内部部署的媒体同步 worker，用于 Pixiv 图片下载任务。

## 职责

- Pixiv 图片下载任务发现与消费
- 维护图片目录 `pixiv_images`，下载后同步 MinIO、提交 tagger
- 通过飞书发送任务状态和失败通知

## 部署形态

本服务默认部署到内部 PaaS/K8s，作为 `port=0` worker 运行，不暴露 HTTP Service。

如果开启 `TAGGER_CALLBACK_SERVER_ENABLED=true`，本服务会同时启动一个内部 HTTP callback 入口，不再是纯 `port=0` worker。PaaS app 需要配置端口并创建 Service；tagger entry 的 callback URL 需要通过 gateway/internal reachable 地址打到该 Service。

## 存储

worker 的全部状态都在集群内：

- 集群 Mongo 的 `chiwei_pixiv` 库，经 `PIXIV_MONGO_*` 连接，启动时必须连上，索引由启动代码创建：
  - `pixiv_images`：图片目录的唯一来源。consumer 下载完成后，把 server 返回的 OSS key（`tos_file_name`）和 tag、作者等元信息一次写入。lark-service 回写的 `image_key` / `width` / `height` / `update_time` 和 tagger projection 写的 `tagger_*` 不会被覆盖。lark-service 发图、MinIO 同步和 tagger projection 都读这个集合。
  - `pixiv_download_tasks`：下载队列。
  - `pixiv_tag_translations`：tag 翻译表。
  - `pixiv_author_downloads`：作者节流，文档形如 `{_id: <作者 ID>, last_download_at: <unix 秒>}`。
- Dynamic Config（经 PaaS API 修改）：
  - `pixiv_version`：pixiv ajax 请求的 `version` 参数，未配置时请求不带这个参数。
  - `pixiv_ban_illusts`、`pixiv_ban_users`、`pixiv_skip_words`：名单，值是 JSON 字符串数组，`"[]"` 表示空名单。key 缺失、不是合法 JSON 或不是字符串数组时，当前步骤失败，不会当成空名单：发现阶段整轮失败并发飞书通知；consumer 不领取任务，退避后重试。
  - `pixiv_cookie`、`pixiv_user_agent`、`pixiv_sec_ch_ua`：pixiv 鉴权头，随请求带给 server。

Tagger 结果另存：打标结果写入 `TAGGER_RESULT_MONGO_*` 指向的 Mongo，复用 channel-server 已在使用的本地 Mongo 基础设施。

## 必需环境变量

- `PIXIV_MONGO_HOST`（可包含端口；包含端口时不再读取 `PIXIV_MONGO_PORT`）
- `PIXIV_MONGO_USERNAME` / `PIXIV_MONGO_PASSWORD`
- `APP_ID`
- `APP_SECRET`
- `SELF_CHAT_ID`
- `HTTP_SECRET` 或 `PROXY_HTTP_SECRET`

## 可选环境变量

- `PIXIV_MONGO_PORT`、`PIXIV_MONGO_DATABASE`（默认 `chiwei_pixiv`）、`PIXIV_MONGO_AUTH_SOURCE`（默认 `admin`）、`PIXIV_MONGO_CONNECT_TIMEOUT_MS`（默认 `2000`）。
- `PIXIV_MONGO_SOCKET_TIMEOUT_MS`：socket 不活动超时，默认 `60000`；`0` 表示不设，此时半开连接上的命令会永久挂起。
- `DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS`：扫描每个外部操作的等待上限，默认 `180000` 毫秒；关注列表按整次分页计时，大规模关注列表可调高。非法超时配置会报错。
- 扫描日志 `discovery_step` 包含作者 ID、调用阶段和耗时。作者失败后继续下一位；只有全部候选入队或确认已存在才更新作者扫描时间（没有新作品也算成功）。超时会阻止查重后的新写入，但已发送的 Mongo 写入可能迟到完成，最终扫描时间写入超时的结果可能未知。

- `DOWNLOAD_CRON`：Pixiv 下载任务 cron 表达式，默认 `12 10 * * *`（每天 10:12）。
- `DOWNLOAD_AFTER_ILLUST_INFO_DELAY_MS`：取作品信息后的等待时间，默认 `1500`（旧值减半）。
- `DOWNLOAD_BEFORE_PAGE_DOWNLOAD_DELAY_MS`：单页图片代理下载前等待时间，默认 `1000`（旧值减半）。
- `DOWNLOAD_AFTER_TASK_DELAY_MS`：每个下载任务完成后的等待时间，默认 `2500`（旧值减半）。
- `DOWNLOAD_AFTER_AUTHOR_DELAY_MS`：作者发现阶段处理完一个作者后的等待时间，默认 `1500`（旧值减半）。
- `DOWNLOAD_LIMITER_COOLDOWN_MS`：每 60 个下载任务后的冷却时间，默认 `120000`（旧值减半）。
- `MINIO_SYNC_ENABLED`：开启 OSS→MinIO per-page 同步，默认关闭。
- `MINIO_SYNC_TIMEOUT_MS`：单张 OSS→MinIO 同步硬超时，默认 `30000`。
- `TAGGER_RESULT_MONGO_ENABLED`：开启本地 Mongo 结果库连接，默认关闭。
- `TAGGER_RESULT_MONGO_*`：本地 tagger 结果 Mongo 配置。
- `TAGGER_TRIGGER_ENABLED`：开启下载后写入 tagger outbox，由后台 worker 自动提交 tagger，默认关闭。
- `TAGGER_ENTRY_URL` / `TAGGER_API_TOKEN`：tagger entry 调用地址和 caller token。
- `TAGGER_SUBMIT_BATCH_SIZE`：每个 tagger task 合并提交的图片数，默认 `1`；线上可设为 `4` 让 tagger-entry 对多图做一次 batch 推理。
- `TAGGER_TRIGGER_WORKER_IDLE_DELAY_MS` / `TAGGER_TRIGGER_RETRY_DELAY_MS` / `TAGGER_TRIGGER_PROCESSING_TIMEOUT_MS` / `TAGGER_TRIGGER_MAX_ATTEMPTS`：tagger outbox worker 轮询、重试和 processing 超时配置。
- `TAGGER_SUBMITTED_RECONCILE_*`：submitted 任务主动查询 tagger-service 的等待、lease 和重试间隔，用于补偿丢失 callback。
- `TAGGER_PROJECTION_ENABLED`：开启当前 callback 结果向 `pixiv_images` 的 durable 投影。
- `TAGGER_PROJECTION_BACKFILL_ENABLED`：允许识别并投影历史 completed 结果，默认关闭，必须与当前结果投影分开授权。
- `TAGGER_PROJECTION_*`：结果投影的 batch、轮询、重试和 processing 超时配置。
- `TAGGER_CALLBACK_URL` / `TAGGER_CALLBACK_AUTH_TOKEN`：tagger-service 回调地址和回调鉴权 token。
- `TAGGER_CALLBACK_SERVER_ENABLED` / `TAGGER_CALLBACK_PORT`：开启 worker 内部 HTTP callback 入口及端口。

## 部署验证开关

- `DISABLE_SCHEDULES=true`：不注册定时任务
- `DISABLE_CONSUMER=true`：不启动下载任务消费者

两个开关同时打开时，进程会保持存活，便于先验证镜像、Pod、环境变量和基础连接。

- `RUN_CONNECTIVITY_CHECK=true`：在两个开关同时打开时，检查集群 Mongo 连通性、逐个读取并解析上面的 Dynamic Config key（结果打在日志里，失败不退出）、检查 OSS 和 MinIO，不启动定时任务或消费者。

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
