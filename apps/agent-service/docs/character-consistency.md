# 人设一致性与上下文架构

> 2026-03-21 · feat/character-consistency-merge

## 核心问题

赤尾是**一个人**（MANIFESTO），但之前的记忆系统是按聊天分裂的：每个群/私聊各一份日记，互不可见。群A聊完动画去群B完全不知道。日程（Schedule）是赤尾级的，但记忆（Diary）是 per-chat 的——"她打算做什么"统一，"她记得什么"分裂。

另一个问题是**反馈循环**：上下文中的具体话题（如"草莓大福"）会被模型在对话中反复提及，对话又被记录进日记，日记又注入下一次对话，形成信息茧房。

## 实体全景

```mermaid
graph TD
    subgraph "原始数据"
        CM[ConversationMessage<br/>每条飞书消息]
    end

    subgraph "素材层（per-chat）"
        DE[DiaryEntry<br/>per-chat per-day<br/>聊天记录的主观摘要]
        PI[PersonImpression<br/>per-chat per-user<br/>对某人的感觉]
        CI[ChatImpression<br/>per-chat<br/>群的氛围/性格]
    end

    subgraph "赤尾级（统一）"
        JD[AkaoJournal daily<br/>每天一篇<br/>融合所有聊天的主观经历]
        JW[AkaoJournal weekly<br/>每周一篇<br/>7篇日志的沉淀]
        SD[AkaoSchedule daily<br/>今天的状态/活动<br/>一天即焚]
        SW[AkaoSchedule weekly<br/>本周节奏方向]
        SM[AkaoSchedule monthly<br/>本月生活基调]
    end

    CM -->|01:00 按chat聚合| DE
    DE -->|后处理| PI
    DE -->|后处理 仅群聊| CI
    DE -->|02:00 所有chat日记| JD
    SD -->|当天计划作为输入| JD
    JD -->|7篇合成| JW
    JD -->|03:00 + 周计划 + persona + web| SD
    JW -->|+ 月计划| SW
    SW -->|方向输入| SD
    SM -->|方向输入| SW
```

## 生产链（夜间处理）

```mermaid
sequenceDiagram
    participant Cron
    participant DiaryWorker
    participant JournalWorker
    participant ScheduleWorker

    Note over Cron: CST 01:00
    Cron->>DiaryWorker: cron_generate_diaries
    DiaryWorker->>DiaryWorker: 为每个活跃chat生成DiaryEntry
    DiaryWorker->>DiaryWorker: 提取PersonImpression
    DiaryWorker->>DiaryWorker: 提取ChatImpression（仅群聊）

    Note over Cron: CST 02:00
    Cron->>JournalWorker: cron_generate_journal
    JournalWorker->>JournalWorker: 收集所有chat的DiaryEntry
    JournalWorker->>JournalWorker: 融合 + 模糊化 → AkaoJournal(daily)

    Note over Cron: CST 03:00
    Cron->>ScheduleWorker: cron_generate_daily_plan
    ScheduleWorker->>ScheduleWorker: Journal(昨天) + WeeklyPlan + persona_core + web_search
    ScheduleWorker->>ScheduleWorker: → AkaoSchedule(daily, 今天)
```

## 消费链（聊天时注入）

```mermaid
graph LR
    subgraph "System Prompt"
        P[persona<br/>Langfuse main prompt]
        S[Schedule today<br/>她的状态/活动]
        CI2[ChatImpression<br/>这个群的氛围]
        PI2[PersonImpression<br/>对在场的人的感觉]
    end

    P --> SP[System Prompt]
    S --> SP
    CI2 -->|群聊| SP
    PI2 -->|群聊| SP

    subgraph "不注入"
        X1[历史日记 ❌]
        X2[历史日志 ❌]
        X3[周/月计划 ❌]
    end
```

**群聊**: persona + Schedule(today) + ChatImpression + PersonImpression

**私聊**: persona + Schedule(today) + CrossGroupImpression

## 反馈循环防护

每经过一层实体传递，具体细节自然衰减一级：

```mermaid
graph LR
    A["DiaryEntry<br/>具体事件<br/>「聊了草莓大福」"] -->|合成时模糊化| B["Journal<br/>模糊话题<br/>「和朋友聊了不少吃的」"]
    B -->|生成计划时抽象| C["Schedule<br/>只有状态<br/>「心情不错」"]

    style A fill:#ff9999
    style B fill:#ffcc99
    style C fill:#99cc99
```

这就是宣言里的 **鲜明 → 模糊 → 印象 → 遗忘**，不靠计数器，靠信息在实体间流转时的自然抽象化。

**关键规则**：
- **Journal prompt**：模糊化具体话题，保留情感和氛围
- **Schedule prompt**：描述状态/活动，**不描述话题**（这是她的生活，不是聊天预案）
- **ChatImpression prompt**：只记氛围/性格，**不记近期话题**
- **PersonImpression prompt**：记对人的感觉，不记具体事件

## 实体对照表

| 实体 | 级别 | Key | 注入聊天 | 用途 |
|------|------|-----|---------|------|
| DiaryEntry | per-chat | (chat_id, date) | ❌ | 素材：喂给Journal和Impression |
| PersonImpression | per-chat per-user | (chat_id, user_id) | ✅ 群聊 | 她对某人什么感觉 |
| ChatImpression | per-chat | (chat_id) | ✅ 群聊 | 这个群什么氛围 |
| AkaoJournal (daily) | 赤尾级 | (date) | ❌ | 沉淀：喂给下一天Schedule |
| AkaoJournal (weekly) | 赤尾级 | (week_start) | ❌ | 沉淀：喂给下周WeeklyPlan |
| AkaoSchedule (daily) | 赤尾级 | (date) | ✅ | 驱动：唯一的记忆来源 |
| AkaoSchedule (weekly) | 赤尾级 | (week_start) | ❌ | 方向：喂给DailyPlan |
| AkaoSchedule (monthly) | 赤尾级 | (month_start) | ❌ | 方向：喂给WeeklyPlan |
| WeeklyReview | per-chat | (chat_id, week) | ❌ | 保留生成，不再注入 |

## Cron 时序（CST）

| 时间 | 任务 | 输入 | 输出 |
|------|------|------|------|
| 01:00 每天 | DiaryEntry + Impressions | Messages | DiaryEntry, PersonImpression, ChatImpression |
| 02:00 每天 | Journal 合成 | 所有DiaryEntry + Schedule(today) | AkaoJournal(daily) |
| 03:00 每天 | Schedule 生成 | Journal(昨天) + WeeklyPlan + persona + web | AkaoSchedule(daily, 今天) |
| 02:30 周一 | WeeklyReview (per-chat) | 本周DiaryEntry | WeeklyReview |
| 02:45 周一 | Weekly Journal | 本周daily Journals | AkaoJournal(weekly) |
| 23:00 周日 | Weekly Plan | MonthlyPlan + prev WeeklyPlan | AkaoSchedule(weekly) |
| 02:00 每月1号 | Monthly Plan | prev MonthlyPlan + persona | AkaoSchedule(monthly) |

## Langfuse Prompts

| Prompt | 版本 | 用途 |
|--------|------|------|
| `journal_generation` | v1 | 日志合成：模糊化话题，保留情感 |
| `journal_weekly` | v1 | 周日志：一周的感性沉淀 |
| `chat_impression_extraction` | v1 | 群氛围提取：只记感觉不记话题 |
| `schedule_daily` | v5 | 日计划：改用Journal输入，描述状态不描述话题 |
| `diary_generation` | v5 | 日记生成（不变） |
| `diary_extract_impressions` | v3 | 人物印象提取（不变） |
