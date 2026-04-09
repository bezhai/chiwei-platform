# Unified Voice Pipeline — 合并内心独白 + 漂移示例

**Status:** ✅ 已实现（2026-04-09, v1.0.0.207）

**Goal:** 将 inner_monologue 和 drift/reply_style 两条独立管线合并为一条，一次 LLM 调用同时生成情绪描写和风格示例，确保两者语义一致。

---

## 架构对比

### Before: 两条独立管线

```
                    ┌─────────────────────────────┐
                    │      inner_monologue.py      │
                    │   cron: 每小时 :30           │
                    │   prompt: "inner_monologue"  │
                    │   input: LE状态+Schedule+碎片│
                    │   output: 情绪描写           │
                    │   storage: inner_monologue_log│
                    └──────────┬──────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │    main prompt       │
                    │  {{inner_monologue}} │ ← 情绪和示例
                    │  {{reply_style}}     │   各自独立生成
                    └──────────────────────┘   互不知道对方
                               ▲
                               │
┌──────────────────────────────┴─────────────────────────────┐
│                  identity_drift.py                          │
│  ┌─────────────────┐     ┌──────────────────┐              │
│  │ base cron        │     │ event-driven      │              │
│  │ 8:00/14:00/18:00│     │ 每次回复后触发    │              │
│  │ prompt:          │     │ observer→generator │              │
│  │ drift_base_gen   │     │ 两阶段 LLM       │              │
│  └────────┬────────┘     └────────┬──────────┘              │
│           └───────────┬───────────┘                         │
│                       ▼                                     │
│              reply_style_log                                │
└─────────────────────────────────────────────────────────────┘
```

**问题：** 情绪说"困得要命"，示例却可能是精力充沛时生成的 → 割裂。

### After: 统一管线

```
                    ┌─────────────────────────────────────┐
                    │         voice_generator.py           │
                    │   prompt: "voice_generator"          │
                    │   input: LE状态 + Schedule + 碎片    │
                    │        + (可选) 近期消息/回复        │
                    │   output: 情绪描写 + 风格示例        │
                    │   storage: reply_style_log           │
                    └──────────┬──────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                                 ▼
   ┌──────────────────┐              ┌──────────────────┐
   │  voice_worker.py  │              │ identity_drift.py │
   │  cron: 每小时整点 │              │ event-driven      │
   │  source="cron"    │              │ source="drift"    │
   │  (无 recent_ctx)  │              │ (传入 recent_ctx) │
   └──────────────────┘              └──────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │    main prompt       │
                    │  {{voice_content}}   │ ← 一次生成
                    │                      │   天然一致
                    └──────────────────────┘
```

**解决：** 情绪和示例在同一次 LLM 调用中生成，保证语义一致。

---

## 变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `app/services/voice_generator.py` | 统一生成函数 `generate_voice()` |
| 新建 | `app/workers/voice_worker.py` | 统一 cron 入口 |
| 修改 | `app/services/identity_drift.py` | `_run_drift` 改为调用 `generate_voice`，删除 observer/generator/base 函数 |
| 修改 | `app/services/bot_context.py` | `reply_style` + `inner_monologue` → `voice_content` |
| 修改 | `app/agents/domains/main/agent.py` | prompt 注入改为 `voice_content` |
| 修改 | `app/workers/unified_worker.py` | 两个 cron → 一个 cron |
| 删除 | `app/services/inner_monologue.py` | 被 voice_generator 吸收 |
| 删除 | `app/workers/monologue_worker.py` | 被 voice_worker 替代 |
| 删除 | `app/workers/base_style_worker.py` | 被 voice_worker 替代 |

**净效果：** 10 files changed, 81 insertions, 254 deletions（净减 173 行）

---

## Langfuse Prompts

| Prompt | 状态 | 说明 |
|--------|------|------|
| `voice_generator` v1 | staging | 新统一 prompt |
| `main` v95 | staging | `<voice>` 改为 `{{voice_content}}` |
| `inner_monologue` v1 | production（待退役） | 旧内心独白 prompt |
| `drift_base_generator` v3 | production（待退役） | 旧基线风格 prompt |
| `drift_observer` v3 | production（待退役） | 旧漂移观察 prompt |
| `drift_generator` v4 | production（待退役） | 旧漂移生成 prompt |

---

## 触发频率

| 路径 | Before | After |
|------|--------|-------|
| 情绪刷新 | 每小时 :30（inner_monologue cron） | 每小时 :00（voice cron） |
| 基线示例 | 8:00/14:00/18:00（base_style cron） | 合并入 voice cron |
| 事件驱动 | observer→generator 两阶段 LLM | generate_voice 一次 LLM |
