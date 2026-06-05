# 赤尾自主化重设计 · 阶段 1A:world 退成推演者 + 角色自主 + 去掉 room_id

落地 [chiwei-world-life-autonomy-redesign.md](chiwei-world-life-autonomy-redesign.md) 的阶段 1A,范式核心一刀。地基(CST / 上下文 / session PG)已在阶段 0 落地。**本版按五工具重写,作废之前基于 room_id 的版本。**

## Problem

现状错了两层。表层:world 是导演——`move_persona` 直接挪角色、角色 `raise_intent` 申请、world 在 prompt 里裁决批准。深层更要命:整个世界用 **room_id 离散建模**——`RoomPresence` 表记"谁在哪个 room",`emit_event` 按"同 room"机械投递,移动是"挪到某 room_id"。这把 world 降成一个查 presence 表、按 ID 相等分发的路由器。可 world 本该是个会思考的脑子:赤尾在厨房煎蛋、绫奈刚进门谁闻得到这股香味,是 world **推演**出来的,不是 `room_id == "kitchen"` 查出来的。

## Goal

world 退成**世界推演者**:用一段自然语言 `detail` 维护"世界此刻什么样"(谁大概在哪、在干嘛、什么氛围),推演"这一刻谁感知得到什么"并 `notify` 给够得着的角色,绝不替角色决定行动。角色**完全自主**:`act` 自己做事(自然语言)、`update_life_state` 维护自己状态。系统里彻底没有 room_id / presence 表 / move_persona / raise_intent / 同 room 机械匹配——**位置只活在 world 的 detail 叙述里,感知只由 world 推演**。

## Non-goals

- 不做 1B(自排唤醒)/ 1C(角色间直连交流)/ 阶段 2(订阅 / ask world / 跨天 memory)。
- 感知的客观性(notify 不夹带情绪 / 指令)本刀只靠 prompt 约束,结构化升级推后。
- 不动阶段 0 的 CST 时间、上下文三层归位、session PG。
- 不动 world 的 heartbeat / self-wake 调度节奏(那是 1B)。

## 五个工具(已与 bezhai 对齐)

**world:**
- `notify(recipients, observation)` — world 推演出"谁此刻够得着这条客观动静",把 `observation`(自然语言客观描述)投给 `recipients`。
- `update_world(detail)` — 写一段自然语言、记"世界此刻什么样",存成 durable 快照。
- `sleep(seconds)` — 定下次多久再醒。

**life(角色):**
- `act(description)` — 自主做一件影响外部世界的事(自然语言,"我去厨房做饭"),汇给 world。
- `update_life_state(current_state, response_mood, activity_type)` — 更新自己的主观状态。

## Key design decisions

**1. 世界状态 = world 的一段自然语言 detail,没有任何离散结构。** `update_world(detail)` 写"世界此刻什么样",durable 存一份(落到 `WorldState`,语义从空着的 `situation` 变成 world 的世界叙述)。**删掉 `RoomPresence` / room_id / `personas_in_room`** ——谁在哪不再是一行 `presence[persona]=room`,而是融在 detail 这段叙述里("赤尾在厨房忙活,绫奈刚到家,千凪还在学校")。world 推演时从上一版 detail + 自己的连续意识流知道世界长什么样。

**2. 感知由 world 推演、定向投递,不靠同 room 匹配。** `notify(recipients, observation)`:world 自己判断"这条客观动静此刻谁够得着"(绫奈进了门够得着厨房的香味、千凪在学校够不着),把 observation 投给推演出的 recipients。**删 `emit_event` 的按-room 广播** ——recipients 是 world 这个脑子推演的结果,不是 `room_id` 相等筛出来的。投递本身复用现有信箱(observation 落进 recipients 的信箱、唤醒对应 life)。

**3. 角色自主 act,world 只推演客观结果、不批准。** `act(description)` 是角色自然语言做的事("我去厨房做饭"),汇给 world;world 在推演里消化它的**客观结果**(她到了厨房 / 被什么挡住),更新 detail,再 notify 该感知到的人。world **绝不批准 / 拒绝她想不想做** ——它只推演"客观上发生了什么",连"她到没到厨房"也是推演(几乎总到,除非客观世界里有冲突)。act 替掉 raise_intent,语义从"申请待裁决"变成"她做了、世界推演结果"。

**4. 角色状态与世界状态对称。** `update_life_state`(角色每轮记"我什么样")和 `update_world`(world 每轮记"世界什么样")严格对称——都是自然语言、都 durable、都是各自主体维护自己那一份真相。角色不读 world 的 detail(信息差命门),world 不替角色写 life_state。

**5. 冷启 world 推演初始世界。** world 没有上一版 detail(首次醒 / 清库)时,按作息推演"此刻世界大致什么样"(三姐妹各在哪、在干嘛),`update_world` 落第一版 detail。不再有 move_persona 放置。这是 world 推演世界客观状态的本职,不是导演。

**6. 五工具,旧导演 / room 机制全删。** world `[move_persona, emit_event, sleep]` → `[notify, update_world, sleep]`;life `[update_life_state, raise_intent]` → `[update_life_state, act]`。连带删 `RoomPresence` / `IntentRaised` / `personas_in_room` / `list_recent_intents` / room_id 一切痕迹。

## Caller coverage

**删:**
- `app/world/tools.py`:`move_persona`、`emit_event` → 换 `notify` + `update_world`
- `app/world/state.py`:`RoomPresence`、`set_presence`、`read_presence`、`personas_in_room` → 全删(世界无 presence)
- `app/nodes/life_tools.py`:`raise_intent_tool` → 换 `act`
- `app/domain/world_events.py`:`raise_intent`、`IntentRaised` → 换"角色动作汇 world"的机制
- `app/data/queries/intents.py`:`list_recent_intents` → 换 world 读 act 批次
- `app/wiring/life_dataflow.py`:`IntentRaised` durable 链 → act 汇 world 的链

**改:**
- `app/world/engine.py`:`_run_world_round` 循环重构(读上版 detail + 续接意识流 → 推演世界 → `update_world` → 对收到的 act 推演客观结果 → `notify` 够得着的角色 → `sleep`);`_presence_text` 删(world 从 detail 知道世界,不再拼 presence);`_wake_reason_text` 冷启段改"推演初始世界";`_world_loop_messages` 喂上版 detail 不喂 presence;`world_loop_instruction` 改成五工具新范式;`WorldState.situation` → `detail`
- `app/nodes/life_wake.py`:感知读取适配(读 notify 投来的 observation);`act` 工具接入

**不动:** `notify` → 信箱 → `EventArrived` → life 唤醒投递链(复用 `deliver_event` / 信箱,只是上游"谁是 recipients"从 room 匹配变 world 推演);session PG;CST helper;world heartbeat / self-wake。

## Data & deployment impact

- `WorldState`:`situation`(现空着)→ `detail`(world 的自然语言世界叙述),durable。
- **删表**:`RoomPresence`(data_room_presence)、`IntentRaised`(data_intent_raised)。coe 隔离库可重建,prod 阶段 1 未上线、无迁移负担。
- **角色 act 汇 world**:一个"角色动作"Data + durable 唤醒 world(占 IntentRaised 链的位置,语义重构为"她做的事";消费靠 world 的 turn 幂等 + 意识流,不上独立状态机——去掉 presence 后没有"被重复写"的硬伤)。走 framework 新 Data 三步检查。
- **感知投递**:`notify` 复用 `EventEnvelope` 信箱(observation 投进 recipients 信箱),deliver / 唤醒链不变。
- **langfuse**:world / life prompt 大改(五工具新范式),发验证泳道 label(coe-world-life2)、与代码原子对齐。
- **部署**:agent-service + vectorize-worker 同步;coe-world-life2 清库冷启验证。

## Tasks

**Task 1 — world 推演者核心(update_world + notify + 删 room/presence)。** 目标:world 用自然语言 detail 维护世界、推演谁够得着就 notify,无 room_id / presence。产出:`update_world(detail)`(落 `WorldState.detail` durable)+ `notify(recipients, observation)`(world 推演 recipients、投信箱);删 `move_persona` / `emit_event` / `RoomPresence` / `personas_in_room` / `_presence_text`;`_run_world_round` 重构为"读上版 detail + 续接 → 推演 → update_world → notify → sleep";冷启推演初始 detail。验收:单测 + coe——world 醒来产出一段 detail 并 durable、下轮读回续上;world 把一条 observation notify 给它推演指定的角色、投进其信箱;全链路 grep 无 room_id / presence / move_persona。

**Task 2 — 角色自主 act + 汇 world(删 raise_intent)。** 目标:角色用 act 自主做事、汇给 world 推演,不再申请裁决。产出:`act(description)` 工具(替 raise_intent_tool)+ 角色动作 Data 与 durable 唤醒 world 的链(替 IntentRaised 链)+ `update_life_state` 保留;world 醒来能读到这批 act 并在推演里消化;删 `raise_intent` / `IntentRaised` / `list_recent_intents` 净尽。验收:单测——角色 act 产出动作、唤醒 world 一轮、world 推演里读到这条 act;raise_intent / 裁决 / intent 语义 grep 零残留;life 工具集只剩 update_life_state + act。

**Task 3 — prompt / 模板 + 闭环重测 + coe 验证。** 目标:world / life prompt 与 langfuse 模板按五工具新范式对齐,闭环跑通完整自主循环。产出:`world_loop_instruction` 与相关 langfuse 模板改成五工具范式(world 推演世界 + notify、角色 act 自主)、发验证泳道 label 与代码原子对齐;`test_world_life_closed_loop` 等按新链路重写;coe-world-life2 清库冷启验证。验收:coe trace 看到完整自主循环——world 冷启推演世界 detail → notify 角色 → 角色感知后自主 act → world 推演客观结果 + 更新 detail + notify → 角色继续,全程无 room_id / presence / move_persona / raise_intent、world 不替角色决定。
