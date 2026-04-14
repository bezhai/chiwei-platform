# 赤尾系统设计

> 最后更新: 2026-04-14

赤尾是一个有完整内在世界的虚拟人，不是聊天机器人。设计哲学见 `MANIFESTO.md`。

三姐妹——赤尾（akao）、千凪（chinagi）、绫奈（ayana）——共享架构，各自独立人格。

---

## 全局数据流

赤尾的 agent 系统由三个循环驱动：**活着**（Life Engine 每分钟 tick）、**回复**（被@时响应）、**沉淀**（记忆的产生与压缩）。

```mermaid
flowchart TB
    subgraph living["🫀 活着 · Life Engine 每分钟 tick"]
        direction LR
        SCHED["📅 日程手帐\n每天 05:00 生成"]
        TICK["⏱️ tick\n加载日程 + 时间线"]
        STATE["📍 当前状态\nactivity · mood"]
        TICK --> STATE
        SCHED --> TICK
        STATE -->|"browsing"| GLIMPSE["👀 窥屏\n翻群消息"]
    end

    subgraph responding["💬 回复 · 被@或私聊时"]
        direction LR
        MSG["📩 消息到达"]
        SAFE["🛡️ 安全检测"]
        CTX["🧠 组装意识\n人格 · 状态 · 日程\n碎片 · 关系 · 风格"]
        AGENT["🤖 Agent\n推理 + 工具"]
        REPLY["📤 回复"]
        MSG --> SAFE --> CTX --> AGENT --> REPLY
    end

    subgraph settling["🌙 沉淀 · 记忆的产生与压缩"]
        direction LR
        CONV["💬 conversation\n对话后 5min 回味"]
        GLIM["👀 glimpse\n窥屏时的印象"]
        DAILY["🌙 daily\n03:00 做梦压缩"]
        WKLY["📖 weekly\n周一 04:00 再压缩"]
        CONV & GLIM --> DAILY --> WKLY
    end

    %% 循环之间的数据流
    STATE -->|"注入此刻状态"| CTX
    SCHED -->|"注入今日安排"| CTX
    REPLY -->|"触发回味"| CONV
    GLIMPSE -->|"有意思才记"| GLIM
    DAILY & WKLY -->|"注入远期记忆"| CTX
    CONV & GLIM -->|"注入今日碎片"| CTX
    REPLY -.->|"漂移触发"| DRIFT["🎙️ 声音漂移"]
    DRIFT -.->|"注入说话风格"| CTX
    REPLY -.->|"关系提取"| REL["🤝 关系记忆"]
    REL -.->|"注入对人的了解"| CTX

    style living fill:#f0f7ff,stroke:#4a9eff
    style responding fill:#fff8e1,stroke:#ff9800
    style settling fill:#f3e5f5,stroke:#9c27b0
```

---

## 日程生成 · Agent Team

每天 05:00 为三姐妹各生成一份日程手帐，作为 Life Engine 一天的行动纲领。

```mermaid
flowchart LR
    subgraph shared["共享层 · 跑一次"]
        W["🌐 Wild Agents x4\n互联网 · 城市\n兴趣 · 情绪"]
        S["🔍 Search Anchors\n天气 · 新番 · 展览"]
        T["🏠 Sister Theater\n5-6 件家庭事件"]
    end

    subgraph persona["per-persona · x3"]
        C["Curator\n按人格筛选素材"]
        WR["Writer\n写日程手帐"]
        CR{"Critic\n审核质量"}
    end

    W & S & T --> C --> WR --> CR
    CR -->|"不通过 · 最多 3 轮"| WR
    CR -->|"PASS"| OUT["📅 日程入库\nakao_schedule"]

    style shared fill:#f0f7ff,stroke:#4a9eff
    style persona fill:#fff8e1,stroke:#ff9800
    style OUT fill:#e8f5e9
```

日程格式：日记体手帐，6-8 个场景，每场景自然带出小时级时间锚点，覆盖起床到睡觉。

---

## Life Engine · Tick

```mermaid
flowchart TD
    TICK["⏱️ 每分钟 tick"] --> LOAD["加载最新状态\nlife_engine_state"]
    LOAD --> SKIP{"skip_until\n在未来?"}
    SKIP -->|"是"| DONE["💤 跳过"]
    SKIP -->|"否"| CTX["加载日程 + 活动时间线\n+ 最近对话碎片"]
    CTX --> LLM["🤖 LLM 决定\nactivity · mood · wake_me_at"]
    LLM --> SAVE["💾 append-only 持久化"]
    SAVE --> BRW{"activity_type\n== browsing?"}
    BRW -->|"是"| GLIMPSE["👀 Glimpse\n翻群消息 · 记印象 · 可能搭话"]
    BRW -->|"否"| DONE

    AT(("被 @ 了")) -->|"硬中断"| CHAT["💬 进入回复流程\n状态注入上下文"]

    style LLM fill:#4a9eff,color:#fff
    style AT fill:#ff9800,color:#fff
```

被@时 LLM 自然调整语气：睡着了 → *"嗯...干嘛..."*；在外面 → *"在外面呢 晚点说"*。

---

## Chat Pipeline

```mermaid
flowchart LR
    A["📩 消息"] --> B{"前置安全\n封禁词 · 注入\n政治 · NSFW"}
    B -->|"拦截"| X["🚫 安全回复"]
    B -->|"通过"| D["🧠 组装意识"]
    D --> E["🤖 Agent 推理\n可调用工具"]
    E --> F["📤 回复"]
    F -.-> G["后置动作"]

    subgraph post["后置动作 · 异步"]
        G1["安全审核"]
        G2["记忆提取\nafterthougt"]
        G3["声音漂移"]
        G4["关系提取"]
    end
    G -.-> G1 & G2 & G3 & G4

    style X fill:#f66,color:#fff
    style E fill:#4a9eff,color:#fff
    style post fill:#f5f5f5,stroke:#999
```

### 意识组装

| 区块 | 来源 | 说明 |
|------|------|------|
| 人格内核 | `bot_persona` | 我是谁 |
| 此刻状态 | `life_engine_state` | 我在做什么、什么心情 |
| 今日安排 | `akao_schedule` | 今天的日程手帐 |
| 今日碎片 | `experience_fragment` | 今天的回味和印象（**群聊隐私过滤**） |
| 远期记忆 | daily / weekly 碎片 | 做梦时已自然模糊化 |
| 对人的了解 | `relationship_memory_v2` | core_facts + impressions |
| 说话风格 | Identity Drift | 最新语气特征 |

### 可用工具

| 工具 | 说明 |
|------|------|
| `search_web` | 联网搜索 |
| `generate_image` | DALL-E 3 画图 |
| `recall` | 向量 + BM25 搜索经历碎片 |
| `check_chat_history` | 翻原始聊天记录 |
| `delegate_research` | 委派子 agent 深度研究 |
| `run_skill` / `sandbox` | 技能执行 / 沙箱代码 |

---

## Memory System

```mermaid
flowchart TD
    subgraph realtime["实时 · 事件驱动"]
        R1(("赤尾回复了")) -->|"5min 沉默\n或 15 条累计"| CONV["💬 conversation\n内心独白"]
        R2(("刷手机")) -->|"有意思才记"| GLIM["👀 glimpse\n窥屏印象"]
    end

    subgraph compress["压缩 · cron"]
        CONV & GLIM -->|"03:00"| DAILY["🌙 daily · 做梦\n压缩 + 自然遗忘"]
        DAILY -->|"周一 04:00"| WKLY["📖 weekly\n更多遗忘"]
    end

    subgraph use["使用"]
        direction LR
        CTX["注入意识\n回复时自动带入"]
        RECALL["recall 工具\n向量+BM25 搜索"]
        HIST["check_chat_history\n读原始消息"]
    end

    CONV & GLIM -->|"今日碎片"| CTX
    DAILY & WKLY -->|"远期记忆"| CTX

    style realtime fill:#e8f5e9,stroke:#4caf50
    style compress fill:#f3e5f5,stroke:#9c27b0
    style use fill:#fff8e1,stroke:#ff9800
```

> LLM 就是赤尾的大脑，工程只负责在对的时间把对的素材喂给她。遗忘是 LLM 重新叙述时的自然副产品。

### 隐私过滤

唯一硬规则：**群聊时不暴露其他群和私聊的细节。** 过滤依据是碎片 `source_chat_id`，不是文字内容。daily/weekly 永远可见（做梦时已模糊化）。私聊中所有碎片可见。

---

## Identity · Voice · Relationships

| 子系统 | 触发 | 产出 | 注入位置 |
|--------|------|------|---------|
| **Identity Drift** | 聊天事件 · 300s debounce | 新语气特征（独白风格 + 回复风格） | 意识组装 · 说话风格 |
| **Relationship Memory** | 聊天事件 · 话题过滤后提取 | core_facts + impression_deltas | 意识组装 · 对人的了解 |
| **Glimpse** | Life Engine browsing · 安静时段外 | glimpse 碎片 · 可能主动搭话 | 记忆系统 |

---

## 核心数据模型

```mermaid
erDiagram
    bot_persona ||--o{ life_engine_state : "persona_id"
    bot_persona ||--o{ akao_schedule : "persona_id"
    bot_persona ||--o{ experience_fragment : "persona_id"
    bot_persona ||--o{ relationship_memory_v2 : "persona_id"

    bot_persona {
        string persona_id PK
        string display_name
        text persona_core
    }
    life_engine_state {
        string current_state
        string activity_type
        string response_mood
        datetime skip_until
        datetime created_at
    }
    akao_schedule {
        string plan_type
        string period_start
        text content
    }
    experience_fragment {
        string grain
        string source_chat_id
        text content
    }
    relationship_memory_v2 {
        string target_user_id
        jsonb core_facts
        jsonb impressions
    }
```

---

## 未来里程碑

| 里程碑 | 目标 | 方向 |
|--------|------|------|
| **M1** Life Engine 精度 | 活动切换贴合日程 | 强化 tick prompt 时间比对、调整 wake_me_at 间隔 |
| **M2** 三姐妹差异化 | 日程和互动风格有明显差异 | 验证 persona_core 区分度、per-persona critic |
| **M3** 主动社交 | 赤尾有自己想说的话 | Glimpse 调优、"想分享"触发机制 |
| **M4** 记忆质量 | 记得该记的，忘得自然 | afterthought prompt 调优、dream 压缩质量 |
| **M5** 安全合规 | 覆盖率和精度 | 频率限流、PII 检测 |
| **M6** 可观测性 | 成本追踪和质量分析 | token 成本拆分、Langfuse evaluation 闭环 |
