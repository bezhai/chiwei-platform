# Glimpse 管线重设计

> 日期: 2026-04-07
> 分支: feat/glimpse-pipeline-redesign

## 背景

Glimpse 是赤尾"刷手机"的体验管线。Life Engine 处于 browsing 状态时，赤尾翻看群聊消息、产生感想、写入经历碎片，最终在凌晨由 dream worker 压缩为日记/周记。

当前实现有四个根本问题：

1. **重复观察** — `get_unseen_messages` 用 bot 最后发言做窗口，没新消息时每次返回同一批消息，导致重复写碎片
2. **与 tick 耦合** — glimpse 寄生在 Life Engine tick 里，browsing + wake_me_at 30 分钟后才触发一次，期间新消息看不到
3. **无递进状态** — 每次独立观察，不记得上次看了什么、想了什么
4. **搭话断路** — MQ publish 通了但 MessageRouter 对 proactive 消息返回空 persona 列表，搭话从未成功发出

## 设计决策

| 项 | 决定 | 理由 |
|---|---|---|
| 核心定位 | "刷手机"体验 > 搭话 | Glimpse 是生活体验管线，不是搭话引擎 |
| 调度 | 独立 cron，与 Life Engine 解耦 | Glimpse 是独立行为，不应寄生在 tick 里 |
| 状态 | per (persona, chat) append-only | 可审计，历史可追溯 |
| 搭话 | 修通但 dry-run | 先观察 glimpse 的搭话判断质量，不实际发送 |
| 多群 | 暂不扩展 | 先把单群管线跑通 |
| 碎片量控制 | 不设上限 | 靠 last_seen 去重 + LLM interesting 判断自然控制 |
| Prompt | 不动 | 管线跑通后根据实际输出再调 |

## 架构

### 新增：`glimpse_state` 表

Append-only，每次观察 INSERT 一行。查最新状态取最后一条。

```sql
CREATE TABLE glimpse_state (
    id          SERIAL PRIMARY KEY,
    persona_id  VARCHAR(50) NOT NULL,
    chat_id     VARCHAR(100) NOT NULL,
    last_seen_msg_time BIGINT NOT NULL,    -- 本次看到的最新消息时间戳(ms)
    observation TEXT NOT NULL DEFAULT '',   -- 本次感想（含 want_to_speak 时附带 stimulus）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 调度：独立 cron

新建 `app/workers/glimpse_worker.py`，注册到 `unified_worker.py` 的 cron_jobs。

```
cron(cron_glimpse, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, timeout=120)
```

执行逻辑：

```
cron_glimpse(ctx):
    非 prod 泳道 → return（与 life_engine_worker 一致）
    for persona_id in all_persona_ids:
        查 life_engine_state 最新状态
        activity_type != "browsing" → skip
        run_glimpse(persona_id)
```

### 核心流程：`run_glimpse` 重写

```
run_glimpse(persona_id):
    1. quiet hours 检查（23:00-09:00 CST）→ skip
    2. chat_id = pick_group()（当前单群硬编码）
    3. 从 glimpse_state 读最新记录 → last_seen_msg_time, last_observation
    4. last_bot_reply_time = 该群最近一次 assistant 回复时间
    5. effective_after = max(last_seen_msg_time, last_bot_reply_time)
    6. messages = get_unseen_messages(chat_id, after=effective_after)
    7. 无新消息 → return
    8. 调 glimpse_observe LLM（传入 last_observation 作为上次感想）
    9. 如果 interesting → INSERT experience_fragment
   10. 如果 want_to_speak → 只记录到 glimpse_state.observation 和日志/Langfuse，不发 MQ
   11. INSERT glimpse_state（本次 last_seen_msg_time + observation）
```

**step 5 解释**：如果在两次 glimpse 之间有人 @赤尾触发了正常对话，那段消息已由 afterthought 生成 conversation 碎片。取 max 跳过已参与的对话，只看后续新消息。

### `get_unseen_messages` 改造

当前签名：`get_unseen_messages(chat_id, persona_id, limit=30)`
改为：`get_unseen_messages(chat_id, after: int = 0, limit: int = 30)`

- 不再用子查询找 bot 最后发言，改为直接用 `after` 时间戳过滤
- `persona_id` 参数移除（调用方自己管状态）
- 仍排除 `user_id = "__proactive__"` 的合成消息

注意：`proactive_scanner.py` 中其他函数（`run_proactive_scan` 等）如果也调了 `get_unseen_messages`，需同步适配签名。

### `life_engine.py` 改动

删除 tick 中的 glimpse 触发逻辑（当前 lines 154-161）：

```python
# 删除以下代码：
if new["activity_type"] == "browsing":
    from app.services.glimpse import run_glimpse
    try:
        await run_glimpse(persona_id)
    except Exception as e:
        logger.error(f"[{persona_id}] Glimpse failed: {e}")
```

### 搭话 dry-run

`run_glimpse` 中 `want_to_speak=True` 时：

- 不调 `submit_proactive_request`
- 在 `glimpse_state.observation` 中记录：`"{observation}\n[want_to_speak] stimulus={stimulus}, target={target_message_id}"`
- 日志 `logger.info(f"[{persona_id}] Glimpse want_to_speak (dry-run): {stimulus}")`
- Langfuse trace 中标记 `dry_run=True`

### Admin 手动触发接口

在 `app/api/router.py` 新增：

```
POST /admin/trigger-glimpse?persona_id=xxx
```

- 不检查 browsing 状态（手动触发就是要强制跑）
- 不检查泳道限制
- 返回 glimpse 执行结果（状态字符串 + LLM 决策 JSON）

## 文件改动清单

| 文件 | 改动类型 | 内容 |
|---|---|---|
| `app/orm/memory_models.py` | 修改 | 新增 `GlimpseState` ORM 模型 |
| `app/orm/memory_crud.py` | 修改 | 新增 `get_latest_glimpse_state()` / `insert_glimpse_state()` / `get_last_bot_reply_time()` |
| `app/services/glimpse.py` | 重写 | 新流程：读状态 → 增量消息 → 观察 → 写状态+碎片，want_to_speak dry-run |
| `app/workers/glimpse_worker.py` | 新建 | `cron_glimpse`：查 life engine 状态，browsing 才执行 |
| `app/workers/unified_worker.py` | 修改 | cron_jobs 加 `cron_glimpse` |
| `app/services/life_engine.py` | 修改 | 删除 glimpse 触发逻辑 |
| `app/workers/proactive_scanner.py` | 修改 | `get_unseen_messages` 签名改为 `after` 参数 |
| `app/api/router.py` | 修改 | 新增 `POST /admin/trigger-glimpse` |

不改的文件：message_router.py、proactive_consumer.py、chat-response-worker.ts、Langfuse prompt。

## 验证方式

1. 部署 agent-service 到独立泳道（如 `feat-glimpse-pipeline-redesign`）
2. 调 `POST /admin/trigger-glimpse?persona_id=akao-001` 手动触发
3. 查 `glimpse_state` 表验证状态记录和递进观察
4. 查 `experience_fragment` 表验证碎片产出（grain=glimpse）
5. 查 Langfuse trace `glimpse-observe` 验证 LLM 输入输出（特别是 last_observation 是否正确传入）
6. 验证 want_to_speak 场景：检查 glimpse_state.observation 中是否有 `[want_to_speak]` 记录
