# GPU 打标推理服务——双机异步、按需激活、文件名入参回调出

## Problem

打标现在全靠手动 ssh .206 跑 `scripts/pipeline/run_mvp.py`、产物落本地 jsonl，一次性、靠人盯。要让打标能被平台编排 worker 自动调用、可远程触发、跑完异步回吐结果，需要把 MVP 打标器内核（`run_pipeline`）封成一个常驻、异步、双机协作的服务。

## Goal

一个 FastAPI 服务，同一套代码靠启动参数分化成两个角色：

- **206（对外入口 + qwen）**：收 `{paths: ["5486389_p0.jpg"], 回调 url}`，受理后立即返回 task_id；本地跑 qwen（describe + OCR），同步调 98 拿轻量 tagger，merge 成每图全维度结果，跑完 POST 回调 url。
- **98（轻量后端）**：被 206 同步调用，跑 wd14 / eva02 / anime_rating / phash，同步返回 per-image tags。

服务自己按文件名只读 minio（桶 `pixiv`，key 即 `<id>_pN.ext`，例如 `5486389_p0.jpg`，不带目录前缀）拉图，不写 mongo。模型按需激活、空闲 15min 卸载（qwen 卸载释放 ~10G；98 保持热以保证同步快返回）。sqlite 存异步任务表，进程重启不丢在途任务、回调失败可重试。

可验证：curl 向 206 提交一批文件名 → 立即拿到 task_id → 稍后回调 url 收到一份 JSON，每张图含 wd14 / eva02 / anime / phash / qwen 各字段。

## Non-goals

- 不写 mongo、不查 `img_map` 增量、不懂 pixiv 业务——那是平台编排 worker 的另一份 spec。
- 不做请求级模型选择：维度由部署参数（激活哪些模型）定，单请求固定跑该机全维度。
- 不接入 chiwei-platform 的 PaaS/K8s、不动 paas-engine GPU 调度——服务直接在 .206/.98 进程常驻。
- 不做 catalog 拼装，不在本 spec 内新增 OCR 之外的打标维度。

## Key design decisions

1. **206 当唯一对外入口，而非 98 分发**——because qwen 是最慢最重的本地阶段，放在入口侧让"慢的本地异步 + 快的远程同步"自然串成一个 task；98 因此退化为无状态同步后端，不必管异步/回调/sqlite。
2. **入参传 minio object basename，而非图字节**——because 大批图走 HTTP body 传输开销大；服务端只读 minio 拉图既省带宽，又守住"只读输入、不写业务"的边界。
3. **按需激活 + 空闲超时卸载，而非常驻全模型**——because qwen vLLM 占 ~10G、长期空占不划算；98 的 onnx 小且要同步快返回故保持热。卸载策略按模型成本配置，不一刀切。
4. **同构参数化部署，而非两套代码**——because 两机差异只是"激活哪些模型 + 是否对外异步入口"，共用 `run_pipeline` 内核与 `merge_row`，避免重复实现。
5. **请求固定跑该机全维度**——Chose 部署级激活 over 请求级选模型，because 离线批打标要的就是每图全维度字段，请求级选择是过度设计；将来要单维度补跑再另加。

## 错误、并发与恢复语义

- **任务状态机与恢复**：任务状态显式流转（受理 → 运行 → 待回调 → 完成 / 失败）。进程重启后，处于"运行 / 待回调"的任务一律视为未完成、**整批重跑**（推理无副作用、重跑安全），不做半成品续跑；回调以 task_id 为幂等键，服务侧允许重发、由调用方据此去重。
- **206→98 失败语义**：沿用内核"单能力 error 不崩整批"。98 对某图失败 → 该图 tagger 能力标 error、qwen 字段照常；98 整体超时 / 不可达（重试上限后）→ 该批所有图 tagger 能力标 error，任务仍以"部分结果"完成并回调，不卡死异步任务。重试次数与超时可配。
- **GPU 并发与卸载**：单机 GPU 串行执行，同一时刻只跑一个 batch、其余排队；模型 load/unload 加锁、空闲计时从最后一个任务完成起算、卸载前确认无在途任务——杜绝重复加载与卸载正在推理的模型。
- **背压与上限**：单批文件名数、队列深度设上限，超限拒绝而非 OOM；具体阈值实现时按显存 / 宿主 RAM 实测定（呼应 describe 全量的 RAM/chunk 教训）。
- **结果主键**：以原始文件名（= pixiv_addr）为贯穿 206 / 98 / merge / 回调的稳定主键，去重复用 dedup_ids、结果按主键对齐，避免多页 / 重复 / 坏图隔离时合并错行。

## Caller coverage

内核 `run_pipeline` / `build_stages` / `QwenVllmStage` / `TaggerStage` / `merge_row` / `dedup_ids` 当前只被 `scripts/pipeline/run_mvp.py` 这一个 CLI 调用。

- `run_mvp.py`（CLI）：保留不动，仍可本地批量跑（实验用），不受服务化影响。
- `run_pipeline` 的"每阶段跑完就 unload"语义：新服务**不直接整条复用**——它只跑该机激活的那一组阶段，且阶段的 load/unload 由服务的激活/空闲卸载生命周期接管。需在 task 1 里把"单机跑自己那组阶段 + merge"从 run_pipeline 中分离出来。
- `merge_row` / `dedup_ids`：直接复用，不改。

## Data & deployment impact

- **新增 sqlite（206 本地）**：异步任务表（task_id / 状态 / 入参 path / 回调 url / 结果 / 时间戳）。临时存储、可定期清理，不涉及 ops-db。
- **不动 mongo**，不改任何现有表 / prompt；不触发任何跨服务部署（本服务独立于 chiwei-platform）。
- **minio**：206、98 各需一份只读凭证访问桶 `pixiv`。
- **网络与回调安全**：服务仅绑内网；callback url 限内网网段 allowlist（防 SSRF）。调用方是自家平台编排 worker，鉴权从简（内网 + allowlist），不上重鉴权。
- **部署形态**：206、98 各常驻一个 FastAPI 进程，启动参数区分角色；98 须先停 comfyui 腾出显存。
- **模型权重**：不打进镜像（十几 G 太肿），通过环境变量指向机器本地已下好的目录（如 `TAGGER_QWEN_MODEL_PATH` / `TAGGER_WD14_MODEL_DIR` / `TAGGER_EVA02_MODEL_DIR`），98 需把 tagger onnx 权重同步到位。
- 服务是常驻进程，重启会中断在途推理——靠 sqlite 任务表 + 回调重试保证不丢任务、可恢复。

## Tasks

1. **单机阶段执行内核（常驻 + 空闲卸载）**
   - Goal：把 `run_pipeline` 的多阶段串行拆成"单机只跑自己激活的那组阶段、模型 load 一次后常驻、空闲超时才卸载"。
   - Deliverable：一个可被服务层调用的执行单元，复用 `QwenVllmStage` / `TaggerStage` / `merge_row`，支持 load-once 与 idle-timeout unload。
   - Verification：本地连续两批推理只 load 一次模型；空闲超过阈值后显存释放；再来请求自动重激活；并发两个请求时模型不重复 load、空闲卸载不打断在途推理（显存观测 + 日志为证）。

2. **FastAPI 异步入口（206 角色）**
   - Goal：对外受理异步任务、落 sqlite、跑完回调。
   - Deliverable：提交端点（文件名列表 + 回调 url → task_id）、sqlite 任务表、回调发送（失败可重试）。
   - Verification：curl 提交一批文件名立即拿 task_id；稍后回调 url 收到结果；杀进程重启后处于运行 / 待回调的任务整批重跑、重复回调按 task_id 幂等去重。

3. **轻量后端 + 跨机串联（98 角色 + 206→98）**
   - Goal：98 提供同步推理端点；206 在一个 task 内本地跑 qwen + 同步调 98 拿 tagger 并 merge。
   - Deliverable：98 同步端点 + 206 内的远程调用与结果合并。
   - Verification：206 单请求的回调结果里同时含 qwen 与 wd14/eva02/anime/phash 全字段；98 冷启动与热态各跑一次、延迟可观测；98 不可达时该批 tagger 能力标 error、qwen 字段仍照常回调（部分结果）。

4. **minio 只读取图**
   - Goal：服务按文件名从桶 `pixiv` 拉图，坏图 / 缺图隔离不崩整批。
   - Deliverable：minio 读取适配，替换 `run_mvp` 的 local_path 加载路径。
   - Verification：给真实文件名批次能拉到图并推理；不存在的文件名标 error 且不影响整批其余图。

5. **双机部署与权重就位**
   - Goal：206 / 98 各起服务、权重挂载到位、98 腾显存。
   - Deliverable：两机启动方式（参数区分角色）+ 权重目录约定。
   - Verification：两机服务起来、健康检查通过；端到端跑通一次真实文件名批的提交→回调。
