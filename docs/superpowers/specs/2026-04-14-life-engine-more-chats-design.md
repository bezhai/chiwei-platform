# Life Engine: 扩展主动搭话机会

## 问题

线上观察（2026-04-07 ~ 04-14）发现主动搭话频率极低，四个瓶颈叠加：

1. **DB bug**: `conversation_messages.id` 是 `GENERATED ALWAYS`，SQLAlchemy model 用 `autoincrement=True`，INSERT 时传 None 被 PG 拒绝。4/11 起所有 proactive 提交失败。
2. **状态限制**: `cron_glimpse` 只对 `browsing` 状态的 persona 跑 glimpse，白天大部分时间 persona 在 studying/working/commuting，glimpse LLM 从不触发。
3. **静默时间重叠**: 23:00-09:00 quiet hours 挡住了 glimpse，但 browsing 恰恰集中在深夜/早晨赖床时段。
4. **单群监控**: 只监控 1 个固定群。

## 改动

### 1. 修 DB bug (models.py)

`id` 列改为 `Identity(always=True)` 让 SQLAlchemy 在 INSERT 时使用 DEFAULT。

### 2. 取消静默时间 (glimpse.py)

删除 `QUIET_HOURS`、`_is_quiet` 及相关跳过逻辑。赤尾凌晨刷手机看到有趣消息也应该能说话。

### 3. 非 browsing 状态概率触发 glimpse (cron.py)

| persona 状态 | 行为 |
|---|---|
| browsing | 每次都跑 glimpse（不变） |
| sleeping | 不触发（睡着了） |
| 其他（commuting/studying/working/eating/resting/idle...） | 15% 概率触发，模拟"掏手机瞄一眼" |

15% × 每 5 分钟 ≈ 平均 33 分钟看一次手机。

### 4. 监控两个群 (glimpse.py)

- `oc_a44255e98af05f1359aeb29eeb503536`（现有）
- `oc_54713c53ff0b46cb9579d3695e16cbf8`（新增）

`_pick_group` 返回列表，cron 对每个 persona × 每个群都跑一次 glimpse。

## 不动的东西

- life engine tick 状态机逻辑
- LLM prompt
- 每小时 2 次工程硬限制
- LLM 自我调节机制

## 成本

改后：3 persona × 2 群 × 12次/小时 × ~15% ≈ 每小时 ~11 次 glimpse LLM 调用（offline-model）。
