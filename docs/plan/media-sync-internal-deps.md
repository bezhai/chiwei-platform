# pixiv 鉴权字段注入迁内网（走 Dynamic Config）

## Problem

pixiv 的会话凭证（`cookie`/`user-agent`/`sec-ch-ua`）现在存在公网服务 chiwei_bot_server（`www.yuanzhi.xyz`）的 Redis 里，由它的 `/api/v2/proxy`（图片下载走同一 `buildHeaders`）在请求 pixiv.net 时注入。凭证存在外网、归它的 JWT `setting` 模块管，轮换和管控都在内网够不着的地方。

## Goal

pixiv 鉴权字段改由内网 Dynamic Config 持有，内网 media-sync-worker 运行时读出、随 proxy / 下载请求带给 chiwei_bot_server，server 把调用方带入的鉴权头转发给 pixiv.net。轮换 cookie 只需改 Dynamic Config，不再碰公网服务的 Redis。

## Non-goals

- 不做更大范围的 media sync 收口——image-store 业务逻辑内化到 worker、channel-server 发图功能去留、`img_map`/`trans_map` Mongo 迁内网、瘦下载契约（download 改成只存 OSS）等，全部"可再议"，不在本次（见文末）。
- 不动对象存储（仍阿里云 OSS），不改 download 的存储/写库/去重逻辑——本次只改它和 proxy 的"鉴权头从哪来"。
- 不强行下线 chiwei_bot_server 的 `setting` 模块或它 Redis 里的 cookie——本次只让 proxy/download 优先用调用方带入的鉴权头，Redis 作为过渡期回退。

## Key design decisions

- **鉴权头由调用方带入、server 转发，Redis 降级为逐字段回退**：chiwei_bot_server 的 `buildHeaders` 对 `cookie`/`user-agent`/`sec-ch-ua` **逐字段**判断——带入且非空就用带入值，缺失或空串则回退读自己 Redis。关键：`DynamicConfig.get()` 拉取失败 / 缺 key 时返回空串，必须当"未带入"处理、回退 Redis，**绝不把空 header 发给 pixiv**（"headers 对象存在"不等于三项凭证有效）。保留 Redis 回退而非一刀切，是为过渡期零回归——老调用方不带头仍走旧逻辑，这也天然是回滚路径。内网注入稳定后 Redis 来源可另行退役（属"可再议"）。
- **鉴权字段走 Dynamic Config，不走 env/ConfigBundle**：cookie 是需要频繁轮换的业务级会话凭证，Dynamic Config 运行时可改、10s 生效，契合轮换诉求；env/ConfigBundle 改一次要重新部署，不合适。按 bezhai 指定走 dynamic-config（这偏离仓库"密钥走部署时配置"的默认，随之的约束见 Data & deployment impact）。
- **跨仓契约字段钉死（两仓共享，不留"实现时定"）**：请求体新增可选对象 `pixiv_auth: { cookie?, user_agent?, sec_ch_ua? }`（`/api/v2/proxy` 与下载接口都加）；Dynamic Config 三个 key 为 `pixiv_cookie` / `pixiv_user_agent` / `pixiv_sec_ch_ua`。两仓独立改、字段名即契约，必须先钉死。新增字段属于被签名 body（现有 X-Token = sha256(salt + JSON.stringify(body) + secret)），client 签名与 server guard 看同一 body，故加字段不破坏鉴权——前提是 server 在 guard 前不剥离 / 重写 body。
- **注入点只在 media-sync-worker**：worker 是唯一直接调 `/api/v2/proxy` 和下载接口的内网消费方（channel-server 只用 image-store 列表、不直接调 proxy）。所以本次只在 worker 侧读 Dynamic Config 并注入，channel-server 不动。

## Caller coverage

chiwei_bot_server `/api/v2/proxy` 与图片下载接口的内网调用方（grep 自 `apps/**` `packages/**`）：

- `pixivProxy`（→ `/api/v2/proxy`）→ media-sync-worker 的 getFollowersByTag / getAuthorArtwork / getTagArtwork / getIllustInfo / getIllustPageDetail（pixiv.ts，被 dailyDownload / consumeService 用）。**改**：请求体带上鉴权头。
- `downloadContent`（→ 图片下载，走同一 `buildHeaders`）→ media-sync-worker `getContent`（consumeService 下载每页时）。**改**：请求体带上鉴权头。
- channel-server：只用 `getPixivImages` / `reportLarkUpload`（image-store 接口，不经 `buildHeaders`），**不受影响**。

涉及改动的契约层：`packages/pixiv-client` 的 `pixivProxy` 与 `downloadContent` 增加可选鉴权头入参并放进发往 server 的 body；media-sync-worker 调用处从 Dynamic Config 读值后传入。

## Data & deployment impact

- **新 Dynamic Config keys**：`pixiv_cookie` / `pixiv_user_agent` / `pixiv_sec_ch_ua`（见 Key design decisions）。**上线前置**：先在 Dynamic Config 配好这三项、并把现有值从 chiwei_bot_server Redis 取出灌入。
- **凭证进 Dynamic Config 的取舍与约束**：按 bezhai 指定 cookie 走 Dynamic Config，偏离仓库"密钥走部署时配置"的默认，随之必须满足：(a) **严禁日志打印鉴权头**——现 `proxy.service.ts` 有 `console.log('headers', headers)` 会泄露 cookie，本次必须去掉 / 脱敏；(b) worker 是 cron、无 per-request context，`DynamicConfig` 默认读 `prod` lane——cookie 是单一 pixiv 账号、跨 lane 同值，读 prod 可接受（若要按部署 lane 取值则显式传 laneProvider）。
- **过渡期维持 Redis 初值**：回滚路径依赖公网 Redis 里的旧 cookie 仍有效，cutover 验证稳定前必须继续维护 Redis 里的凭证、不能清。
- **worker 读 Dynamic Config**：复用 `@inner/shared` 的 `DynamicConfig`（默认连 `paas-engine:8080`、10s 缓存）。worker 是内网 k3s 进程，可达 paas-engine。
- **跨仓上线顺序（关键）**：① 先上"proxy/download 优先用带入头、缺失回退 Redis"的 chiwei_bot_server（兼容变更：老 worker 不带头仍走 Redis，零回归）→ ② 在 Dynamic Config 配好三个字段初值 → ③ 再上带注入的 worker。**回滚**：worker 回退到不带头版本，server 自动回退读 Redis，无需协同回滚。
- **部署中断**：部署 media-sync-worker 会中断正在跑的下载消费循环；上线前确认无在跑的批量下载。
- 两仓改动（monorepo + chiwei_bot_server），非 PG / 不涉 ops-db、不涉 Langfuse。

## Tasks

1. **chiwei_bot_server：proxy/download 接受调用方鉴权头**（仓库：chiwei_bot_server）
   - **Goal**：proxy 与下载接口的请求体接受可选 `pixiv_auth`，`buildHeaders` 逐字段优先用带入值、缺失或空回退 Redis。注意下载接口比 proxy 多一层 DTO/service（`DownloadImageDto → downloadImage → proxyRequestBuffer`），鉴权字段必须一路透传到 `buildHeaders`、不能在这层被丢；同时去掉会泄露 cookie 的 header 日志。proxy 的"GET 转发"本质与 download 的存储逻辑都不变。
   - **Deliverable**：chiwei_bot_server proxy + download 模块改造 + 与 monorepo 约定的 `pixiv_auth` 请求体字段。
   - **Verification**：带 `pixiv_auth` 请求 proxy 与下载，server 都用带入头打 pixiv.net 成功（不是 Redis）；不带或字段为空时逐字段回退 Redis、行为与现状一致（零回归）；日志不再出现 cookie。

2. **media-sync-worker：从 Dynamic Config 读鉴权字段并注入**（仓库：monorepo）
   - **Goal**：worker 调 proxy/下载前从 Dynamic Config 读 pixiv 三个鉴权字段，经 pixiv-client 随请求带给 server；`packages/pixiv-client` 的 `pixivProxy`/`downloadContent` 增加鉴权头入参。
   - **Deliverable**：pixiv-client 鉴权头入参 + worker 读 Dynamic Config 并注入的链路。
   - **Verification**：跑一次下载/元数据抓取，server 端收到 worker 带入的鉴权头并请求 pixiv 成功；在 Dynamic Config 改 cookie 后（≤10s）新请求用新值，不再依赖 server Redis 里的旧值。

## 可再议（非本次，仅记录探查结论，未承诺 scope）

下面是"media sync 链路收口"更大范围的探查结论，本次不做，留待另议：

- chiwei_bot_server 的 image-store 业务逻辑（去重、`img_map`/`trans_map` 写入、列表查询、飞书上传回填）收口进内网 media-sync-worker，让 server 只剩 proxy 透传 + 瘦下载（下字节 + 存 OSS + 返回 key）。
- channel-server 的发图功能（`发图`/`换一批`/`查看详情`/每日推图）去留——若收口则交互式发图无法保留（飞书事件只进 channel-server），需产品取舍。
- `img_map`/`trans_map`/`download_task` 这份 `chiwei` 库当前在阿里云外网、worker 与 chiwei_bot_server 共用，迁内网是有状态 owner 切换，需单独的 cutover 与回滚方案。
