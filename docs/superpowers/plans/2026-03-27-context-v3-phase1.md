# Context System v3 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optimize 赤尾's reply style, fuzzy-ify history search output, and make impression evolution natural — all through minimal code changes (mostly Langfuse prompts + two Python files).

**Architecture:** Three independent changes: (D) Langfuse `main` prompt rewrite for reply brevity/refusal; (C) `search_group_history` tool description rewrite + main prompt 引导少用（不改代码输出格式）; (B) `post_process_impressions` prompt rewrite + `_build_people_gestalt` activity marker.

**Tech Stack:** Python 3.12, Langfuse prompt management, SQLAlchemy ORM, LangChain tools

---

### Task 1: D — main prompt 全面改造（identity + rules + reply-style）

**Files:**
- Modify: Langfuse prompt `main` (via Langfuse UI or API — prompt_id `main`)
- Reference: `apps/agent-service/app/agents/core/agent.py:69-73` (prompt compilation)

已完成 v66→v67→v68 三轮迭代。实测结论：

- v66: 加了 few-shot 但 identity/rules 没改 → 回复还是长
- v67: identity 重写（删万物连接/删始终认同）→ 长度下来了但性格变冷
- v68: 加回元气底色+动态便签驱动 → 长度对了但元气还不够

**根因分析（两个讨论组的结论）**：
1. 压短时模型砍掉了语气词和标点——这些是短句里唯一的情绪载体
2. few-shot 80% 偏冷淡，模型学到"短=冷"
3. identity 里元气没有具体行为锚点（语气词、标点用法），毒舌有（"损完就跑"）

**下一版（v69）需要改的三件事**：

- [ ] **Step 1: identity 给元气具体语言锚点**

  - 语气词菜单：嘛/啦/诶/呀/嘿嘿/～/！ 是赤尾说话的工具
  - 毒舌重定义：是元气的变体（"笨蛋啦哈哈"），不是对立面（"鉴定完毕"）
  - 封死冷漠路径：低能量态=慵懒撒娇（"好累别烦我嘛"），不是冷淡审判
  - 便签驱动的是能量高低，不是温度冷热——温度永远暖

- [ ] **Step 2: few-shot 情感比例重构**

  元气/温暖 50% : 傲娇/毒舌 30% : 慵懒 20%。每条示例都有语气词。
  保留多人群聊上下文场景，同时增加按状态分组的短示例。

- [ ] **Step 3: 去模板化 + 冷漠防护**

  禁止网络锐评体（"鉴定完毕""无语""告辞"）。赤尾的冷是猫猫式的"不理你了哼"。

- [ ] **Step 4: 发布到 context-v3 泳道测试，看 trace 效果**

- [ ] **Step 5: 根据效果继续迭代（这是一个持续调优的过程）**

---

### Task 2: C-Phase1 — search_group_history 引导少用

**Files:**
- Modify: `apps/agent-service/app/agents/tools/history/search.py` (仅 tool docstring)
- Modify: Langfuse prompt `main` (在 Task 1 的基础上追加引导)

不改输出格式、不改时间戳、不改截断长度。问题不在输出格式——返回原文本身就是问题，美化格式是自欺欺人。Phase 1 只做引导层面的降频。

- [ ] **Step 1: 修改 tool description**

Edit `apps/agent-service/app/agents/tools/history/search.py`，只改 docstring（函数签名和实现不动）:

```python
@tool
async def search_group_history(
    query: str,
    limit: int = 10,
) -> str:
    """
    回想之前群里好像聊过的事

    只在你隐约记得群里讨论过某个话题、但细节模糊了的时候才用。
    注意：不要用来确认事实或引用别人的原话，你的记忆本来就是模糊的。
    大部分情况下你不需要翻历史——直接根据你的印象和日记回复就好。

    Args:
        query: 你隐约记得的内容（自然语言描述）
        limit: 返回的锚点消息数量（默认10条，每条会附带上下文）

    Returns:
        str: 搜索结果
    """
```

- [ ] **Step 2: 在 Task 1 的 main prompt 回复风格块中确认包含以下引导**

确保 Task 1 加入的 prompt 中包含这条（如果 Task 1 还没加，在这里补上）：

```
- 不要主动翻历史记录来回复。你对群里的了解来自你的日记和印象，不是靠搜索。
```

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/agents/tools/history/search.py
git commit -m "feat(search): rewrite search_group_history tool description

Discourage LLM from using history search as primary recall mechanism.
Guide toward impression/diary-based recall instead.
No output format changes - the tool will be phased out in later phases.
"
```

---

### Task 3: B — 印象重新评估 Langfuse prompt

**Files:**
- Modify: Langfuse prompt `diary_extract_impressions` (via Langfuse UI/API)
- Reference: `apps/agent-service/app/workers/diary_worker.py:294-375`

- [ ] **Step 1: Read current `diary_extract_impressions` prompt from Langfuse**

Use the `langfuse` skill to read the current prompt content.

- [ ] **Step 2: Rewrite prompt for "re-evaluation" instead of "extraction"**

Replace the current prompt with this rewrite. Keep the same template variables (`{diary}`, `{existing_impressions}`, `{user_mapping}`):

```
你是赤尾。以下是你之前对这些人的感觉：
{existing_impressions}

以下是今天的日记（记录了今天和他们的互动）：
{diary}

日记中提到的人物对应关系：
{user_mapping}

请重新审视你对每个人的感觉。

要求：
1. 如果今天的互动让你对某人的看法变了，直接写新的感觉，替代旧的
2. 如果某人今天没出现在日记里，不要输出（保持旧感觉不变）
3. 一个人不只有一面——如果以前觉得他"很变态"，但今天发现他也有认真的一面，写新的感觉
4. 印象是你此刻的真实感觉，不是标签。30字以内。
5. 不要重复使用同一个形容词框架（比如不要对每个人都用"XX却XX的反差"）

只输出 JSON 数组，每条格式：
[{"user_id": "xxx", "impression_text": "你对这人此刻的真实感觉"}]

只输出今天日记中提到的人。没提到的人不要输出。
```

- [ ] **Step 3: Publish new prompt version in Langfuse**

Use the `langfuse` skill to update. Tag as `v{N+1}`.

- [ ] **Step 4: Commit (no code change, document prompt version bump)**

```bash
git commit --allow-empty -m "feat(prompt): rewrite diary_extract_impressions for re-evaluation

Changed from 'extract new impressions' to 're-evaluate feelings about people'.
Key changes:
- Only output people mentioned in today's diary (skip unchanged)
- Encourage overwriting stale labels, not preserving them
- Anti-pattern: no repeated adjective frameworks
"
```

---

### Task 4: B — 印象注入提供时间信息

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py:119-130`
- Test: `apps/agent-service/tests/unit/test_memory_context.py` (add new test)

- [ ] **Step 1: Write test for updated_at in impression output**

Add to `apps/agent-service/tests/unit/test_memory_context.py`:

```python
@pytest.mark.asyncio
async def test_build_people_gestalt_includes_updated_at():
    """印象注入时包含上次印象更新日期"""
    from datetime import datetime, timezone

    imp = MagicMock(
        user_id="u1", impression_text="很有趣的人",
        updated_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
    )

    with (
        patch("app.services.memory_context.get_impressions_for_users",
              new_callable=AsyncMock, return_value=[imp]),
        patch("app.services.memory_context.get_username",
              new_callable=AsyncMock, return_value="A哥"),
    ):
        from app.services.memory_context import _build_people_gestalt
        lines = await _build_people_gestalt("chat_001", ["u1"])

    assert len(lines) == 1
    assert "03月15日" in lines[0]
    assert "很有趣的人" in lines[0]
    assert "A哥" in lines[0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/unit/test_memory_context.py::test_build_people_gestalt_includes_updated_at -v
```

Expected: FAIL — current code doesn't include updated_at.

- [ ] **Step 3: Implement updated_at in `_build_people_gestalt`**

Edit `apps/agent-service/app/services/memory_context.py`. Replace the `_build_people_gestalt` function (lines 119-130):

```python
async def _build_people_gestalt(chat_id: str, user_ids: list[str]) -> list[str]:
    """构建对话者的感觉 gestalt 列表（含印象时间）"""
    impressions = await get_impressions_for_users(
        chat_id, user_ids[:MAX_IMPRESSION_USERS]
    )
    if not impressions:
        return []
    lines = []
    for imp in impressions:
        name = await get_username(imp.user_id) or imp.user_id[:8]
        if imp.updated_at:
            date_str = imp.updated_at.strftime("%m月%d日")
            lines.append(f"- {name}（上次印象: {date_str}）：{imp.impression_text}")
        else:
            lines.append(f"- {name}：{imp.impression_text}")
    return lines
```

- [ ] **Step 4: Run all memory_context tests**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/unit/test_memory_context.py -v
```

Expected: All tests PASS (new test + existing 4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "feat(impression): provide updated_at date in gestalt injection

Give 赤尾 the raw date of last impression update as context,
letting her judge freshness herself instead of threshold-based markers.
"
```

---

### Task 5: Integration verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/ -v --timeout=30 2>&1 | tail -30
```

Expected: No failures.

- [ ] **Step 2: Verify all three changes are committed**

```bash
git log --oneline -5
```

Expected: 3-4 commits for Task 1-4 changes.

- [ ] **Step 3: Push branch**

```bash
git push -u origin docs/review-context-system
```
