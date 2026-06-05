# 平台内 Tagger 触发器

## Problem

`tagger-service` 已验证可以按 MinIO object basename 异步打标并回调结果，但 chiwei-platform 里还没有自动触发器。当前 Pixiv 下载链路只负责把图片写入旧 Mongo / OSS，并 best-effort 同步到自建 MinIO；打标仍需要人工提交文件名。

需要在平台内补齐编排：下载完成后确认 MinIO 对象可读，提交 tagger 任务，接收回调，并把结果落到本地 Mongo，供后续检索、回填、重跑和清洗使用。

## Goal

`media-sync-worker` 在 Pixiv 图片下载并同步 MinIO 成功后，自动提交 tagger 任务；tagger-service 回调后，worker 将任务状态和每图结果写入本地 Mongo。

数据边界明确：

- 旧 Mongo 仍是 Pixiv 下载源数据：`img_map` / `download_task` / `trans_map`。
- 本地 Mongo 是 tagger 结果库，复用 channel-server 已在使用的本地 Mongo 基础设施。
- tagger 结果不写回旧 `img_map`。

## Non-goals

- 不修改 tagger-service 的 API 合约。
- 不把 tagger 结果拆散回写旧 Pixiv 元数据集合。
- 不做旧字段名、大小写、历史 schema 兼容。
- 不引入新的数据库基础设施；本地 Mongo 复用现有平台内 Mongo。
- 不在本轮实现图片标签检索 UI 或搜索排序。

## Key design decisions

1. **触发器放在 `media-sync-worker`**

   该 worker 已经掌握图片下载、旧 Mongo 文档、OSS key 和 MinIO 同步状态。触发器放在这里可以避免额外服务之间再传递 Pixiv 状态。

2. **两套 Mongo 连接并存**

   `media-sync-worker` 当前的 `MONGO_*` 仍用于旧 Pixiv 业务库。新增一组前缀化 env 指向本地 Mongo，用于 tagger 结果库，避免把 worker 主连接切换到本地 Mongo 后破坏现有下载链路。

3. **MinIO 同步成功是提交 tagger 的硬前置**

   tagger-service 只接受 MinIO object basename，不读旧 OSS。现有 best-effort 同步需要拆出可判断结果的内部能力；只有拿到可读 object basename 后才提交打标任务。

4. **结果按原始 row 原样保存**

   tagger-service 回调的 per-image row 是动态 payload。平台侧只加任务状态和索引字段，row 原样保存，不白名单字段、不重命名字段、不从结果里猜兼容结构。

5. **callback 幂等**

   tagger-service 可能因回调失败重试，同一 `task_id` 的 callback 必须可重复接收。写入以 `task_id + pixiv_addr` 去重，并允许同一图片后续被新 task 覆盖最新结果。

6. **worker 增加小 HTTP 入口**

   当前 `media-sync-worker` 是 `port=0` worker，PaaS 不会创建 Service。为了接收裸机 tagger entry 回调，worker 需要变成 worker + 内部 HTTP callback 入口，并由 gateway/internal reachable URL 暴露给 tagger entry。

## Data impact

本地 Mongo 使用独立数据库或独立集合前缀，避免和 channel-server 的 `lark_event` 混在一个业务集合里。

建议集合：

- `tagger_tasks`
  - 记录 tagger task 的提交、回调、失败、重试状态。
  - 以 `task_id` 做唯一键。
  - 保存提交的 `paths`、callback payload 原文、错误信息、时间戳。

- `tagger_image_results`
  - 一图一条最新结果。
  - 以 `pixiv_addr` 做唯一键。
  - 保存 `task_id`、`status`、`result` 原文、错误信息、时间戳。

- `tagger_image_result_events`
  - 可选追加日志，用于保留每次重跑结果。
  - 不作为第一阶段必须项，除非验证阶段确认需要追溯多版本。

## Configuration impact

新增 tagger 触发器配置：

- tagger entry URL
- tagger caller bearer token
- callback base URL
- callback bearer token
- trigger enabled flag
- batch size / timeout / retry 参数

新增本地 Mongo 结果库配置，使用前缀化 env，值指向 channel-server 正在使用的本地 Mongo：

- result Mongo host / port
- result Mongo database
- result Mongo username / password
- result Mongo auth source
- result Mongo connect timeout

这些配置通过 PaaS ConfigBundle / App env / Release env 管理，不直接改 K8s Secret。

## Deployment impact

- `media-sync-worker` 不再是纯 `port=0` worker，需要配置容器端口和 Service。
- 需要给 tagger entry 一个可达 callback URL。
- gateway rule 需要先 explain 再 upsert；生产写操作前单独确认。
- 发布会重启 worker，部署前需要确认没有正在跑的下载任务，或明确接受中断。
- 初始上线必须先走独立泳道验证，不直接部署 prod。

## Error semantics

- MinIO 未启用、未查到 OSS key、同步失败或超时：不提交 tagger，记录可观测日志，不影响原下载任务成功。
- tagger submit 失败：写任务失败状态到本地 Mongo；不回滚旧下载任务。
- callback 鉴权失败：返回 401，不写入结果。
- callback payload 不满足当前明确合约：返回 400，不做兼容猜测。
- 单图 row 有能力级错误：仍保存 row 原文，图片状态为 completed_with_errors 或 completed，由后续读取方解释。

## Caller coverage

第一阶段只覆盖自动下载链路：

- Pixiv 下载消费者：下载图片、写旧 Mongo、同步 MinIO、提交 tagger。
- Tagger callback HTTP 入口：接收 tagger-service 回调并写本地 Mongo。

暂不覆盖手动补跑、历史批量 backfill、UI 查询和搜索排序。

## Tasks

1. **本地 Mongo 结果库接入**
   - Goal：`media-sync-worker` 能同时连接旧 Pixiv Mongo 和本地 tagger 结果 Mongo。
   - Deliverable：结果库 client、集合初始化、索引初始化、任务和图片结果写入接口。
   - Verification：单测覆盖配置解析、索引初始化和幂等 upsert；关闭 tagger 配置时不连接结果库。

2. **MinIO 可判断同步结果**
   - Goal：把现有 best-effort 同步拆出可用于触发判断的结果语义。
   - Deliverable：返回 object basename / skipped / failed / timeout 的同步能力，原 best-effort 行为保持不破坏下载主路径。
   - Verification：单测覆盖 disabled、missing key、success、failure、timeout；现有 best-effort 测试继续通过。

3. **Tagger submit client**
   - Goal：按 tagger-service 当前合约提交 basename paths 和 callback URL。
   - Deliverable：带鉴权、超时、重试、状态落库的 submit client。
   - Verification：单测覆盖成功、401/5xx、超时、批量路径和禁用开关。

4. **Callback HTTP 入口**
   - Goal：`media-sync-worker` 接收 tagger-service callback 并写本地 Mongo。
   - Deliverable：健康检查和 tagger callback route；鉴权、payload 校验、任务和图片结果幂等写入。
   - Verification：单测覆盖鉴权失败、非法 payload、重复 callback、部分错误 row。

5. **下载链路触发接入**
   - Goal：下载成功且 MinIO 对象就绪后自动提交 tagger。
   - Deliverable：Pixiv 下载消费者中的触发调用；失败只影响 tagger 状态，不影响下载任务成功。
   - Verification：单测覆盖下载成功触发、MinIO 未就绪不触发、submit 失败不影响 download success。

6. **部署与泳道验证**
   - Goal：在独立泳道验证 worker + HTTP callback + 本地 Mongo 写入完整链路。
   - Deliverable：README / env example 更新、PaaS app port 变更说明、gateway 配置和回滚说明。
   - Verification：泳道部署后用真实文件名跑提交到回调，确认本地 Mongo 有 task 和 image result；解绑/下线泳道后清理。
