# Multi-Bot Context 设计文档

**日期**：2026-04-03  
**分支**：feat/multi-bot-context  
**目标**：支持多个 persona bot 同时在一个群里，各自有独立的人设、上下文记忆和对话历史视角。

**三姐妹角色设定**：
- **千凪**（ちなぎ）— 姐姐，知心大姐姐，温柔稳重
- **赤尾**（小尾）— 傲娇高中生，现有主角
- **绫奈**（あやな）— 妹妹，懵懂小孩，天真烂漫

---

## 背景与问题

当前 agent-service 完全不区分 bot 身份：

- `build_inner_context()` 不接受 bot_name，所有 bot 共享同一套 context 数据
- `main` prompt 的 `<identity>` 块硬编码"赤尾（小尾）"
- `_DEFAULT_REPLY_STYLE`、错误消息硬编码赤尾名字
- `get_reply_style`、`get_group_culture_gestalt`、`person_impression` 都只按 `chat_id` 索引
- 对话历史不区分"是哪个 bot 说的"

多个 persona bot 进同一个群 → 她们共享 context、人设、记忆，变成"同一个人的多个人格"而不是独立人物。

---

## 设计原则

> **当前 bot = self (assistant)，其他所有发言者（含其他 bot）= others (user)**

这一条规则贯穿所有层：chat 历史构建、impression 生成、gestalt 生成、identity drift。

跨 bot 知识共享只能通过显式对话（如工具调用），不能靠隐式数据渗透。

---

## 数据模型

### 新增 `bot_persona` 表

```sql
CREATE TABLE bot_persona (
    bot_name            VARCHAR PRIMARY KEY REFERENCES bot_config(bot_name),
    display_name        VARCHAR NOT NULL,        -- "赤尾"、"千凪"、"绫奈"
    persona_core        TEXT    NOT NULL,        -- 完整人设（~1000字），注入 main prompt {{identity}}
    persona_lite        TEXT    NOT NULL,        -- 精简人设（~200字），diary/journal worker 用
    default_reply_style TEXT    NOT NULL,        -- reply_style 最终兜底，替换 _DEFAULT_REPLY_STYLE
    error_messages      JSONB   NOT NULL         -- {"guard":"...", "content_filter":"...", "error":"..."}
);
```

`persona_core` 同时服务：
- chat：注入 `main` prompt 的 `{{identity}}` 变量
- schedule_worker：替换 Langfuse `persona_core` prompt

`persona_lite` 服务：
- diary_worker、journal_worker：替换 Langfuse `persona_lite` prompt

**迁移**：赤尾的现有 Langfuse `persona_core`/`persona_lite` 内容迁移到此表，Langfuse 原 prompt 废弃。

---

## Langfuse `main` Prompt 改造

只改 `<identity>` 块，从硬编码改为变量注入：

**改前：**
```
<identity>
你是赤尾（小尾），真实的人类少女。
...（硬编码赤尾人设）...
</identity>
```

**改后：**
```
<identity>
{{identity}}
</identity>
```

运行时 prompt 变量：

| 变量 | 来源 |
|------|------|
| `{{identity}}` | `bot_persona.persona_core`（按 bot_name 查询） |
| `{{reply_style}}` | Redis `reply_style:{chat_id}:{bot_name}` |
| `{{inner_context}}` | `BotContext.build_inner_context()` |
| `{{available_skills}}` | 不变 |

---

## BotContext 类

**位置**：`apps/agent-service/app/services/bot_context.py`

封装所有 per-(chat_id, bot_name) 的上下文数据，替代 `build_inner_context()` 的散装调用。

```python
class BotContext:
    def __init__(self, chat_id: str, bot_name: str, chat_type: str):
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.chat_type = chat_type

    async def load(self) -> None:
        """并行加载所有 per-bot 数据"""
        self.persona, self.reply_style = await asyncio.gather(
            load_bot_persona(self.bot_name),               # DB
            get_reply_style(self.chat_id, self.bot_name),  # Redis
        )

    def get_identity(self) -> str:
        return self.persona.persona_core

    def get_error_message(self, kind: str) -> str:
        name = self.persona.display_name
        return self.persona.error_messages.get(kind, f"{name}遇到了问题")

    def build_chat_history(self, messages: list) -> list:
        """当前 bot → assistant，其余所有发言者 → user（带发言者名字前缀）"""
        result = []
        for msg in messages:
            if msg.bot_name == self.bot_name:
                result.append({"role": "assistant", "content": msg.content})
            else:
                prefix = f"{msg.sender_name}: " if msg.sender_name else ""
                result.append({"role": "user", "content": f"{prefix}{msg.content}"})
        return result

    async def build_inner_context(self, **kwargs) -> str:
        """透传 bot_name 到所有 context 查询"""
        return await build_inner_context(
            chat_id=self.chat_id,
            bot_name=self.bot_name,
            **kwargs
        )
```

---

## Context Key 迁移

所有 per-chat 的 context 存储加 `bot_name` 维度：

| 旧 Key | 新 Key |
|--------|--------|
| `reply_style:{chat_id}` | `reply_style:{chat_id}:{bot_name}` |
| `reply_style:__base__` | `reply_style:__base__:{bot_name}` |
| `person_impression:{chat_id}:{user_id}` | `person_impression:{chat_id}:{user_id}:{bot_name}` |
| `group_culture_gestalt:{chat_id}` | `group_culture_gestalt:{chat_id}:{bot_name}` |

**注意**：`conversation_messages` 表不变，继续共享（所有 bot 的发言都存入同一张表）。`bot_name` 字段已存在，用于历史构建时区分发言身份。

---

## Worker 变更

### diary_worker

**改前**：每个群跑一次，生成 impression/gestalt，key 按 chat_id。  
**改后**：每个群 × 每个 persona bot 各跑一次，key 加 bot_name。

生成时，对话历史里其他 bot 的发言按 "user（带名字）" 处理——她们是群里可观察到的参与者，不是可渗透的内心状态。

### identity_drift

Redis key 改为 `reply_style:{chat_id}:{bot_name}`，每个 bot 独立漂移。

### schedule_worker / journal_worker

从 `bot_persona.persona_core` / `bot_persona.persona_lite` 读人设，不再加载 Langfuse `persona_core`/`persona_lite`。  
Worker 需接收 `bot_name` 参数（当前只为赤尾跑，后续可扩展）。

---

## 硬编码清理

**`apps/agent-service/app/agents/domains/main/agent.py`**：

| 位置 | 改前 | 改后 |
|------|------|------|
| L31 | `"你发了一些赤尾不想讨论的话题呢~"` | `bot_context.get_error_message("guard")` |
| L319 | `"小尾有点不想讨论这个话题呢~"` | `bot_context.get_error_message("content_filter")` |
| L376 | `"赤尾好像遇到了一些问题呢QAQ"` | `bot_context.get_error_message("error")` |

**`apps/agent-service/app/services/memory_context.py`**：

- `_DEFAULT_REPLY_STYLE` 常量删除
- `get_reply_style(chat_id, bot_name)` 最终 fallback 改为 `bot_persona.default_reply_style`

---

## bot_name 透传链路

`bot_name` 已在 MQ 消费者里通过 `header_vars["app_name"]` 注入 context，但未被 context 构建函数使用。

需要将 bot_name 贯穿到：
1. `_build_and_stream()` → 创建 `BotContext` 实例
2. `build_inner_context()` → 接受 bot_name 参数
3. `get_reply_style()` → 接受 bot_name 参数
4. `build_chat_history()` → 由 BotContext 处理
5. `ChatAgent` 初始化 → prompt_id 改从 `bot_persona` 读（默认 `"main"`）
6. diary_worker / identity_drift → 接受 bot_name 参数

**lark-server 侧**：`bot_name` 已在 MQ 消息体里，无需改动。

---

## 不在本次范围内

- bot 间显式通信工具（"姐姐问妹妹"）
- `bot_persona` 的管理 UI / API
- 多 bot 回复协调（@mention 路由已由 `robot_union_id` 处理）
