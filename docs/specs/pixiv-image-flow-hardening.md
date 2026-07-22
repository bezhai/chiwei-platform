# Pixiv 图片链路可靠性闭环

## Problem

当前 Pixiv 图片链路已经从作品发现贯通到本地 Mongo、MinIO、Tagger 和飞书发图，但五处状态边界会造成静默漏图或永久悬挂：

- 关注作者分页会漏掉不足一整页的尾页。
- 单页下载、元数据写入或下载后交接失败时，作品任务仍可能进入 Success。
- 下载后镜像与 Tagger 入队是进程内后台任务，进程退出可丢失；已存在图片又绕过补偿。
- channel-server 在抽中不可用本地候选后直接缩短结果，不继续补位。
- Tagger 提交失败和丢 callback 的任务没有完整恢复路径，结果也没有进入本地图片检索。

## Goal

把 Pixiv 图片从发现到展示收敛为一条可重放、可观测、最终一致的链路：

- 关注作者分页完整覆盖 Pixiv 返回的 total。
- 一个作品只有在所有选中页面都已下载或确认存在，并完成必要的耐久交接后，下载任务才进入 Success。
- 下载后的本地镜像与 Tagger outbox 可幂等重放；进程在任意一步退出后，任务重试或有界对账都能继续推进。
- 发图查询只选择具备展示入口的本地文档，并在候选处理失败时继续补位，直到达到请求数量或候选确实耗尽。
- Tagger 的提交、远端任务、callback 结果和本地图片投影形成闭环；原始 row 保留不变，并成为发图搜索的数据源。

## Non-goals

- 不恢复已经下线的公网 image-store 列表或上传 fallback；channel-server 继续以本地 Mongo 与 MinIO 为唯一图片源。
- 不修改 tagger-service 的 HTTP API，也不为旧字段名、大小写或猜测的历史 row schema 增加兼容层。
- 不根据模型分数自动修改 `visible` 或 `del_flag`；当前没有产品侧审核阈值契约。
- 不改变 GIF、封禁作者、敏感词、R-18 和最多二十页的现有业务规则。
- 不在本轮合码、部署生产或自动启动历史全量回填。

## Key design decisions

1. **分页以已请求 offset 与 total 的关系为准**

   每次请求固定二十四条；只要下一页 offset 小于服务端 total 就继续。整页边界不多请求，非整页尾页不遗漏。

2. **作品 Success 是页级完整性与耐久交接的汇总结果**

   新下载页必须完成代理下载、源 Mongo 元数据写入和下载后交接；已存在页也必须重放同一下载后交接。缺 URL、代理下载失败、元数据写入失败、镜像失败、源文档缺失或 Tagger outbox 入队失败都属于该页失败。并发页面全部 settle 后统一抛出摘要错误，由现有下载任务 Fail/Dead 状态机承接。

3. **镜像先于 durable outbox，重放不重置已完成 Tagger 生命周期**

   下载后处理不再 fire-and-forget。先幂等同步本地图片文档，再确保 Tagger outbox 已存在。重复处理已 submitted/completed 的图片只修复缺失交接，不把结果重新改回 queued。镜像更新保留本地拥有的飞书上传和 Tagger enrichment 字段。

   跨 Mongo 不依赖事务，而是遵守单向 invariant：源元数据完成后，依次完成已启用的本地镜像、durable outbox，最后才允许下载任务 Success。任意两个阶段之间退出，都会由下载任务重领或历史 reconciler 重放。某阶段只有在对应 feature flag 关闭时才是合法省略；已开启但 unavailable、disabled 或返回失败都属于页面失败。Tagger trigger 开启时要求 MinIO 与本地镜像同时开启，配置不自洽时启动失败；Tagger 关闭但 MinIO 开启时，直接 MinIO 同步也属于必须等待的阶段。

4. **历史缺口通过显式开启的有界 reconciler 修复**

   新增带持久 cursor 的源图片 reconciler。它以不可变且唯一的源 `_id` 为单次扫描顺序，扫描全集而不是只扫描当时已满足 OSS 条件的文档；到达尾部后完成一个 epoch，并在受控间隔后从头开始下一 epoch，因此游标越过后才变得可用的旧文档不会永久遗漏。单个实例通过带 fencing token 的 lease 拥有 cursor；过期 owner 不能推进新 owner 的进度。

   每个符合条件的图片调用与在线下载相同的幂等后处理。失败项先写入独立的 durable retry 状态再允许主 cursor 前进，避免毒数据永久阻塞全局进度；retry 自身也带 lease，进程在 processing 中退出后可由新 owner 接管，且主 cursor 不得清除仍有效的 retry lease。retry 成功或明确被人工处置前不会丢失。该 worker 默认关闭，部署时需单独评估存量规模、Tagger 容量和泳道数据边界后再开启，避免代码发布即触发历史全量推理。

5. **频道补位发生在本地候选层，不回退公网**

   候选必须有非空 `pixiv_addr`，并且已有 `image_key` 或非空 `tos_file_name`。候选单位是唯一 `pixiv_addr`；重复源文档优先选择已有 image key 的代表，否则选择最新的有效对象 key。上传层记录本次已尝试地址，失败后继续取未尝试候选。

   顺序查询只在首次定位时应用 page offset，后续使用包含唯一 `_id` 的稳定排序 continuation，不能同时扩大 skip 和排除已尝试项。随机查询按地址无放回抽样，查询不到未尝试地址时才算耗尽。显式 `pixiv_addrs` 先按首次出现去重，再严格按输入顺序处理，只在指定集合内降级，绝不混入无关图片。

6. **Tagger 结果投影复用现有结果表作为 durable outbox**

   callback 先按图片原样保存 row，并写独立的 projection 状态，最后才提交 task 完成状态。投影 worker 将原始 row、任务元数据和从动态 payload 提取的通用字符串检索词写入所有同 `pixiv_addr` 的本地图片文档。零匹配不创建半文档，而是使用有上限的退避持续等待镜像补齐，并在连续失败达到告警阈值后持续告警而非静默终止。

   在线 callback 新产生的 pending projection 始终处理；“已有 result 但没有 projection 状态”的历史识别由独立 backfill 开关控制，默认关闭，并与历史图片 reconciler 共用有界批量和显式授权边界。派生检索索引最多保留五百一十二个去重字符串叶子，单项最多五百一十二个字符，最多遍历十六层；限制只作用于派生索引，原始 row 必须完整保存。

7. **提交失败与远端 submitted 对账使用两套独立恢复语义**

   网络、超时和 5xx 提交失败进入 outbox 退避重试，4xx 或达到最大次数才进入终态。长时间 submitted 的 task 通过 tagger-service 现有任务查询接口主动对账，状态严格按当前小写契约转换：

   - `accepted` / `running`：释放 lease 并延后复查。
   - `pending_callback` / `completed` 且有 result：复用 callback 落库。
   - `failed` 且有 result：按 callback 投递耗尽处理，仍复用 callback 落库。
   - `failed` 且无 result，或明确 404：关联图片进入新 generation 的 retry。
   - 传输错误 / 5xx：只释放 lease 并延后；未知状态、缺必填字段或不自洽的 result 视为协议错误并告警，不猜测别名。

   远端 submit 返回后，先持久化包含 generation 与 processing lease 的 `registering` task，再条件化提交各图片 owner，最后发布为 `submitted`。进程在任一步退出时，submitted reconciler 会先补完过期的 `registering` 过渡态；callback 在过渡态只确认接收，随后从远端任务查询接口取回保留的 result，避免出现“图片 submitted 但 task 不存在”的永久悬挂。

   空 rows 或缺少 task 预期 path 的部分 rows 不能直接提交 task 完成标记；每个预期图片必须已经持久化 row，或被当前 owner 明确判定为 stale，整批 task 才能 commit。终态后的迟到 callback 仍按 generation fencing 判断，不因 task 已结束而覆盖新 owner。

8. **不伪造远端提交的 exactly-once**

   tagger submit 当前没有幂等键，响应超时后的跨轮重投可能产生重复远端任务。本轮承诺 at-least-once 提交与幂等结果/投影，不宣称不会重复推理。

9. **每张图片使用单调 generation 和 current owner 防止旧结果覆盖**

   每次在已有远端 task 失败后重新提交，图片 generation 单调增加；当前远端 `task_id` 是该 generation 的 owner token。task 记录其每张图片的 generation，callback、submitted 对账、重新入队、projection 完成和 lease 释放都必须同时匹配 generation、owner 与 lease fencing token。迟到 callback 可以保留为 task 事件，但只要不再拥有图片就标记为 stale，不得覆盖当前 result、重排当前图片或清除新 generation 的 pending projection。task commit marker 只在全部 row 已持久化或被 fencing 判定 stale 后写入。

## State and data impact

继续使用现有数据库，不引入新基础设施：

- 旧 `chiwei.img_map` 仍是图片源数据。
- 本地 `chiwei_pixiv.pixiv_images` 增加 Tagger 原始结果、任务状态、独立更新时间和检索词；不改图片创建/更新时间来伪造“今日新图”。
- `chiwei_tagger.tagger_image_results` 在原有 trigger 状态之外增加独立 projection 状态、尝试次数、lease 和下次执行时间；历史完成结果可自动进入投影。
- `chiwei_tagger.tagger_tasks` 增加 `registering` 可恢复过渡态、每图 generation/processing lease、submitted 对账 lease、下次对账时间和错误信息；callback 最后更新 task，作为整批结果持久化或 stale 判定完成的 commit marker。
- 本地镜像库增加一个小型 reconciler cursor 文档；cursor 只记录进度，不复制业务 payload。

所有新写入均为幂等 upsert/update。Tagger row 保存在 `tagger_result` 中时不白名单字段、不重命名字段；检索词只是对原始动态 payload 的通用字符串叶子索引，不替代原始结果。

## Configuration and deployment impact

- 新增历史 post-download reconciler 的 enabled、batch size 和 idle delay 配置，默认关闭。
- 新增 Tagger submitted 对账等待时间、投影 retry/processing timeout 和 projection 历史 backfill 开关等运行参数；历史 backfill 默认关闭，Tagger 功能关闭时不启动对应 worker。
- 新索引先以后台方式创建并验证完成，再允许开启存量扫描；存量扫描与投影的 batch 上限必须可配置，不能与下载高峰争抢无界资源。
- media-sync-worker 发布会中断当前下载、trigger、projection 和 reconcile 循环；上线前必须确认在途任务并走独立 `coe-*` 泳道。
- 该 worker 的测试泳道若复用生产源 Mongo/OSS/MinIO，会产生真实状态竞争和写入；验证必须使用隔离数据，或关闭 schedules、download consumer、Tagger trigger、projection 和历史 reconciler，仅做无副作用启动检查。
- 历史 reconciler 首次生产启用是单独的有状态操作，不随代码发布自动执行；需要在确认存量、批大小和 Tagger 容量后再授权。

## Caller coverage

- 关注作者发现：每日下载任务经 media-sync-worker Pixiv wrapper 调用 PixivClient。
- 下载任务：新图片、已存在图片、多页部分失败、后处理失败、Running 回收重试和 Dead 状态。
- 下载后处理：本地镜像、直接 MinIO 模式、Tagger outbox 模式和历史 reconciler。
- Tagger：submit、trigger worker、callback HTTP、submitted 主动对账、projection worker、重复 callback 和旧完成结果补投影。
- 发图：用户命令、换一批、每日一图、今日新图都允许补位；查看详情只在显式地址集合内保序降级。
- 搜索：原 Pixiv 作者/标题/标签继续生效，并增加 Tagger 动态检索词。

## Tasks

1. **关注作者分页完整性**
   - Goal：任何 total 都完整请求所有关注作者且不多请求空尾页。
   - Deliverable：修正分页终止条件并补齐整页、非整页和生产规模 total 的测试。
   - Verification：total 为 24、25、48、457 时分别请求 1、2、2、20 页，offset 连续且结果完整。

2. **下载任务成功边界与同步后处理**
   - Goal：页面失败或必要后处理失败时，作品任务不能进入 Success；重试可从已存在源图片继续修复。
   - Deliverable：页级结果汇总错误、可等待的幂等后处理、已存在页重放和镜像 enrichment 保留。
   - Verification：新页成功、已存在页成功、部分页失败、镜像失败、outbox 失败与进程重试场景都有自动化测试；只有完整作品可走 Success。

3. **历史 post-download reconciliation**
   - Goal：无需重置已成功下载任务，也能有界补齐历史本地镜像与 Tagger outbox。
   - Deliverable：默认关闭、按 epoch 扫描全集、带 fencing 的持久 cursor、独立失败重试状态和配置说明。
   - Verification：重复执行不重置 Tagger 状态；两个 owner 竞争时旧 owner 不能推进 cursor；毒数据不阻塞后续且不会丢失；已越过 cursor 的旧文档后来满足条件时可在下一 epoch 被处理。

4. **频道候选资格与失败补位**
   - Goal：可用图片存在时，不因首批候选元数据缺失、MinIO 缺对象或上传失败而误返回空结果。
   - Deliverable：唯一地址候选、稳定 continuation、随机无放回、失败补位循环和显式地址查询边界。
   - Verification：随机/顺序查询都能跨失败候选凑满目标；page 大于一时不跳候选；重复地址只尝试一次；候选耗尽时返回实际可用数量；详情查询去重保序且不混入其他地址。

5. **Tagger 提交与 submitted 对账**
   - Goal：暂时性提交失败可恢复，丢 callback 的远端任务最终能取回结果或重新入队。
   - Deliverable：与结果投影共享 generation/current-owner 契约的可重试性分类、退避/终态、远端任务查询 client、submitted lease 与对账 worker。
   - Verification：网络/5xx、4xx、重试耗尽、五种远端状态及其 result 组合、404、无效 payload、空/部分 rows、迟到 callback 和 lease fencing 竞态均有测试。

6. **Tagger 结果 durable 投影与搜索消费**
   - Goal：当前和历史 Tagger 结果最终进入本地图片文档，并可被发图搜索命中。
   - Deliverable：与 submitted 对账共享 generation/current-owner 契约的 projection outbox、幂等本地投影、有界动态检索词、独立历史 backfill 开关和 channel-server 搜索接入。
   - Verification：原始 row 字段逐项保留；旧 generation 不能覆盖新结果；重复本地文档全部更新；零匹配退避并告警；只有开启授权后旧 completed 结果才补投影；搜索能命中真实 v1 tag、描述和 OCR 文本。

7. **整体验证与运维交接**
   - Goal：确认五项修复覆盖所有调用方，并给出安全的隔离验证和历史启用方式。
   - Deliverable：分层测试结果、类型检查、调用方反查、配置/README 更新和回滚说明。
   - Verification：依赖可用环境中相关 Bun 测试与 TypeScript 检查通过；tagger-service 任务查询契约测试通过；未执行未经授权的部署或生产写入。
