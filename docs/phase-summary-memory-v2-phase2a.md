# Phase 2a 完成总结：Journal 层 + Schedule 素材多样化

**完成日期：** 2026-03-26
**分支：** `perf/deep-memory-optimize`
**泳道：** `mem-v2`（已部署，dev bot 已绑定）

---

## 目标回顾

补上 Journal 中间层，建立完整的夜间管线：

```
DiaryEntry(per-chat) → Journal(赤尾级) → Schedule daily
```

Journal 把各群具体话题转化为赤尾的个人感受，作为 Schedule 的情感输入，替代原来直接注入的 recent_diary。

Schedule 的世界素材从 2 个固定 query 改为 8 个维度池随机选取 4-6 个，引入多样性。

---

## 实施内容

### 代码（6 commits）

| Commit | 内容 |
|--------|------|
| `8594a82` | AkaoJournal ORM 模型 + 4 个 CRUD 函数 |
| `ab4a85e` | journal_worker：daily + weekly 生成，ARQ cron 入口 |
| `8aee23a` | schedule_worker：8 维度池 + `_select_dimensions`（日期作随机种子） |
| `a0895e4` | schedule_worker：接入 `yesterday_journal`，传入 `active_dimensions` |
| `0f97179` | unified_worker：注册 journal cron，时序调整 |
| `348d769` | 回溯脚本 `scripts/backfill_journals.py` |

### 夜间管线时序（CST）

```
03:00  cron_generate_diaries         → DiaryEntry + Impression
04:00  cron_generate_daily_journal   → Journal daily（新增）
04:30  cron_generate_weekly_reviews  → WeeklyReview（周一）
04:45  cron_generate_weekly_journal  → Journal weekly（周一，新增）
05:00  cron_generate_daily_plan      → Schedule daily（原 03:30）
23:00  cron_generate_weekly_plan     → Schedule weekly（周日）
02:00  cron_generate_monthly_plan    → Schedule monthly（月初1号）
```

### Langfuse Prompts

| Prompt | 版本 | 变化 |
|--------|------|------|
| `journal_generation` | v2（新建） | 生成 daily journal：融合多群日记 → 赤尾个人感受 |
| `journal_weekly` | v2（新建） | 生成 weekly journal：7 篇 daily journal → 周感受 |
| `schedule_daily` | v6（更新） | 新增 `{{active_dimensions}}`，用 `{{yesterday_journal}}` 替代 `{{recent_diary}}` |

### 数据库

```sql
-- 已手动创建
CREATE TABLE akao_journal (
    id SERIAL PRIMARY KEY,
    journal_type VARCHAR(10) NOT NULL,   -- "daily" | "weekly"
    journal_date VARCHAR(10) NOT NULL,   -- ISO 日期，weekly 为周一
    content TEXT NOT NULL,
    model VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (journal_type, journal_date)
);
```

---

## 架构变化

### 数据流（before → after）

**Before:**
```
Schedule daily 输入：月计划 + 周计划 + recent_diary(per-chat) + 世界素材(2个固定query)
```

**After:**
```
Schedule daily 输入：周计划 + yesterday_journal(赤尾级) + active_dimensions + 世界素材(4-6维度随机)
```

### 隐私层级

```
DiaryEntry: 具体（"陈儒推荐了《夜樱家》"）
   ↓ journal_worker 模糊化
Journal:    感受（"和朋友聊了有趣的新番"）
   ↓ schedule_worker 注入
Schedule:   状态（"今晚想补一下最近的番"）
   ↓ memory_context 注入聊天
Agent:      行为（谈话时自然提到追番的期待）
```

---

## 待验证

1. **手动触发 daily journal 生成**（覆盖过去几天的历史数据）：
   ```bash
   # 在容器内或本地连 DB
   uv run python -m scripts.backfill_journals --start 2026-03-20 --end 2026-03-25
   ```
2. **观察 Schedule daily 生成质量**：journal 输入是否比 raw diary 更清晰
3. **观察素材多样性**：连续 7 天的手帐，不同日期维度是否有变化

---

## 下一阶段：Phase 2b

**聊天注入重构** — `memory_context.py` 改造

- `build_inner_context()` 替代 `build_user_context()` + `build_schedule_context()`
- `inner_context` 注入 agent，替代两个分散的变量
- Langfuse `main` prompt 变量同步更新
- 删除 `inner_state.py`

**Phase 2c（可并行）**

`load_memory` tool 升级 — `recent`/`topic` 模式，diary 返回摘要而非原文
