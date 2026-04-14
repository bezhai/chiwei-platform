# 赤尾系统设计

> 最后更新: 2026-04-14

赤尾是一个有完整内在世界的虚拟人，不是聊天机器人。设计哲学见 `MANIFESTO.md`。

三姐妹——赤尾（akao）、千凪（chinagi）、绫奈（ayana）——共享架构，各自独立人格。

---

## 全局架构

```mermaid
flowchart LR
    subgraph ext["飞书"]
        MSG((消息))
    end

    subgraph gateway["入口层"]
        LP["lark-proxy\nwebhook"]
        LS["lark-server\n消息处理"]
    end

    subgraph brain["agent-service"]
        direction TB
        CHAT["💬 Chat Pipeline"]
        SAFE["🛡️ Safety Guards"]
        MEM["🧠 Memory"]
        LIFE["🫀 Life Engine"]
        SCHED["📅 Schedule"]
        GLIMPSE["👀 Glimpse"]
        VOICE["🎙️ Identity & Voice"]
        REL["🤝 Relationships"]
    end

    subgraph store["存储层"]
        PG[("PostgreSQL")]
        QD[("Qdrant")]
        RD[("Redis")]
        MQ["RabbitMQ"]
        LF["Langfuse"]
    end

    MSG --> LP --> LS --> CHAT
    CHAT --> MQ --> LS --> MSG

    CHAT -.- MEM & LIFE & VOICE & REL
    LIFE -.- SCHED & GLIMPSE
    brain --- PG & QD & RD & LF
```

---

## Chat Pipeline

```mermaid
flowchart LR
    A["📩 消息到达"] --> B["解析"]
    B --> C{"前置安全\n封禁词 · 注入\n政治 · NSFW"}
    C -->|拦截| X["🚫 安全回复"]
    C -->|通过| D["构建上下文"]
    D --> E["🤖 Agent\n流式推理 + 工具"]
    E --> F["📤 发送回复"]
    F -.-> G["后置动作\n安全审核 · 记忆提取 · 漂移"]

    style X fill:#f66,color:#fff
    style E fill:#4a9eff,color:#fff
```

### 上下文注入

Agent 回复时，以下信息被组装为"赤尾的意识"：

```mermaid
flowchart LR
    subgraph ctx["赤尾的意识"]
        direction TB
        WHO["👤 人格内核"]
        NOW["📍 此刻状态"]
        TODAY["📅 今日安排"]
        BRAIN["💭 今天的碎片"]
        FAR["📖 日记 · 周记"]
        PERSON["🤝 对人的了解"]
        STYLE["🎙️ 说话风格"]
    end

    BRAIN --> FILTER{"群聊?"}
    FILTER -->|"是"| RULE["只看本群碎片\ndaily/weekly 可见"]
    FILTER -->|"私聊"| ALL["所有碎片可见"]

    style ctx fill:#f0f7ff,stroke:#4a9eff
```

### 可用工具

| 工具 | 说明 |
|------|------|
| `search_web` | 联网搜索 |
| `generate_image` | DALL-E 3 画图 |
| `recall` | 向量 + BM25 混合搜索经历碎片 |
| `check_chat_history` | 翻原始聊天记录 |
| `delegate_research` | 委派子 agent 深度研究 |
| `run_skill` / `sandbox` | 技能执行 / 沙箱代码 |

---

## Memory System

```mermaid
flowchart TD
    subgraph trigger["记忆怎么产生"]
        REPLY(("赤尾\n回复了"))
        BROWSE(("赤尾\n刷手机"))
    end

    REPLY -->|"5min 沉默\n或 15 条累计"| CONV["💬 conversation\n对话后的内心独白"]
    BROWSE -->|"有意思才记"| GLIM["👀 glimpse\n窥屏印象"]

    CONV & GLIM -->|"03:00 做梦\n压缩 + 自然遗忘"| DAILY["🌙 daily\n睡前日记"]
    DAILY -->|"周一 04:00\n更多遗忘"| WKLY["📖 weekly\n周记"]

    subgraph recall_box["想不起来时"]
        RECALL["🔍 recall\n向量+BM25"]
        HIST["📜 check_chat_history\n读原始消息"]
    end

    style CONV fill:#e8f5e9
    style GLIM fill:#e8f5e9
    style DAILY fill:#fff3e0
    style WKLY fill:#fce4ec
```

> LLM 就是赤尾的大脑，工程只负责在对的时间把对的素材喂给她。遗忘是 LLM 重新叙述时的自然副产品，不需要 TTL 或删除。

---

## Life Engine

赤尾不是等消息的机器人，她有自己的生活节律。

```mermaid
flowchart TD
    TICK["⏱️ 每分钟 tick"] --> LOAD["加载最新状态"]
    LOAD --> SKIP{"skip_until\n在未来?"}
    SKIP -->|"是"| DONE["💤 跳过"]
    SKIP -->|"否"| CTX["加载日程 + 时间线\n+ 最近对话"]
    CTX --> LLM["🤖 LLM 决定\nactivity · mood · wake_me_at"]
    LLM --> SAVE["💾 持久化\nappend-only"]
    SAVE --> BRW{"browsing?"}
    BRW -->|"是"| GLIMPSE["👀 触发 Glimpse"]
    BRW -->|"否"| DONE

    AT(("被 @ 了")) -->|"硬中断\n不管在干嘛"| CHAT["💬 Chat Pipeline\n状态注入上下文"]

    style LLM fill:#4a9eff,color:#fff
    style AT fill:#ff9800,color:#fff
```

被@时 LLM 自然调整语气：睡着了 → *"嗯...干嘛..."*；在外面 → *"在外面呢 晚点说"*。

---

## Schedule Generation

每天 05:00 生成三姐妹日程。

```mermaid
flowchart LR
    subgraph shared["共享层 · 跑一次"]
        W["🌐 Wild Agents x4\n互联网 · 城市 · 兴趣 · 情绪"]
        S["🔍 Search Anchors\n天气 · 新番 · 展览"]
        T["🏠 Sister Theater\n5-6 件家庭事件"]
    end

    subgraph persona["per-persona · x3"]
        C["Curator\n按人格筛选"]
        WR["Writer\n写日程手帐"]
        CR{"Critic\n审核质量"}
    end

    W & S & T --> C --> WR --> CR
    CR -->|"不通过\n最多 3 轮"| WR
    CR -->|"PASS"| OUT["📅 日程入库"]

    style shared fill:#f0f7ff,stroke:#4a9eff
    style persona fill:#fff8e1,stroke:#ff9800
    style OUT fill:#e8f5e9
```

日程格式：日记体手帐，6-8 个场景，每场景带小时级时间锚点，覆盖起床到睡觉。

---

## Glimpse · Identity · Relationships

```mermaid
flowchart LR
    subgraph glimpse["👀 Glimpse"]
        direction TB
        G1["browsing 状态触发"]
        G2["选白名单群 · 读增量消息"]
        G3["LLM 观察"]
        G4["有意思 → glimpse 碎片\n想搭话 → 主动发言"]
        G1 --> G2 --> G3 --> G4
    end

    subgraph voice["🎙️ Identity Drift"]
        direction TB
        V1["聊天事件"]
        V2["300s debounce"]
        V3["LLM 生成新语气"]
        V4["内心独白风格\n+ 回复说话风格"]
        V1 --> V2 --> V3 --> V4
    end

    subgraph rel["🤝 Relationships"]
        direction TB
        R1["话题过滤"]
        R2["提取 core_facts\n+ impression_deltas"]
        R3["注入聊天上下文"]
        R1 --> R2 --> R3
    end

    style glimpse fill:#f0f7ff,stroke:#4a9eff
    style voice fill:#fff8e1,stroke:#ff9800
    style rel fill:#fce4ec,stroke:#e91e63
```

- **Glimpse**：23:00-09:00 安静时段不触发，主动发言每小时上限 2 条
- **Identity Drift**：赤尾的说话方式会被身边的人自然影响
- **Relationships**：让赤尾记得每个人的事实和印象

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
