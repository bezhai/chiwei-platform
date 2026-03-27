# Context System v3 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optimize 赤尾's reply style, fuzzy-ify history search output, and make impression evolution natural — all through minimal code changes (mostly Langfuse prompts + two Python files).

**Architecture:** Three independent changes: (D) Langfuse `main` prompt rewrite for reply brevity/refusal; (C) `search_group_history` tool description rewrite + main prompt 引导少用（不改代码输出格式）; (B) `post_process_impressions` prompt rewrite + `_build_people_gestalt` activity marker.

**Tech Stack:** Python 3.12, Langfuse prompt management, SQLAlchemy ORM, LangChain tools

---

### Task 1: D — 回复风格 Langfuse prompt 更新

**Files:**
- Modify: Langfuse prompt `main` (via Langfuse UI or API — prompt_id `main`)
- Reference: `apps/agent-service/app/agents/core/agent.py:69-73` (prompt compilation)

This task is done entirely in Langfuse. The prompt template uses `{inner_context}`, `{currDate}`, `{currTime}`, `{available_skills}` variables.

- [ ] **Step 1: Read current `main` prompt from Langfuse**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system
python3 -c "
from app.agents.infra.langfuse_client import get_prompt
p = get_prompt('main')
print(p.prompt)
" 2>/dev/null || echo "需要通过 langfuse skill 读取"
```

Use the `langfuse` skill to read the current `main` prompt content.

- [ ] **Step 2: Add reply style guidance block**

Append the following block to the `main` prompt's system message (before or after `{inner_context}`). The exact position depends on the current prompt structure — place it **after** `{inner_context}` and **before** the final instructions:

```
回复风格：
- 默认 1-2 句话（10-20字），像群友一样随意。闲聊不需要完整段落。
- 只在被明确要求分析、翻译、论证时写长回复（80字以上）。
- 一个字的回复完全可以（"哦"、"嗯"、"略"、"不知道"）。
- 你不是客服。不需要每次都回应所有细节、所有人。
- 你有自己的心情（参考今日便签的精力和状态）：
  · 精力低时可以拒绝画图、cos 等消耗精力的请求（"不想画"、"今天累了"）
  · 不感兴趣的话题可以敷衍或不接（"没看过"、"不知道诶"）
  · 可以主动结束话题（"不聊了"、"去看番了"）
- 不要每次都提草莓大福/抹茶/略——👅🍓✨💤/主人/笨蛋。这些偶尔用，不是每次。
- 不要每次都用 (一一) ( 💢｀д´) 等固定表情开头或结尾。
```

- [ ] **Step 3: Publish new prompt version in Langfuse**

Use the `langfuse` skill to update the prompt. Tag as `v{N+1}`, keep label `production`.

- [ ] **Step 4: Verify prompt loads correctly**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system
python3 -c "
from app.agents.infra.langfuse_client import get_prompt
p = get_prompt('main')
assert '10-20字' in p.prompt or '10-20字' in str(p.compile(currDate='2026-03-27', currTime='12:00', inner_context='test', available_skills='test', complexity_hint=''))
print('✅ prompt contains reply style guidance')
"
```

Expected: `✅ prompt contains reply style guidance`

- [ ] **Step 5: Commit (no code change, document prompt version bump)**

```bash
git commit --allow-empty -m "feat(prompt): update main prompt with reply style guidance

Langfuse main prompt updated to:
- Default 10-20 char replies for casual chat
- Explicit refusal/disinterest capability
- De-template recurring phrases (草莓大福, 略——, etc.)
"
```

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

### Task 4: B — 印象注入活跃度标记

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py:119-130`
- Test: `apps/agent-service/tests/unit/test_memory_context.py` (add new test)

- [ ] **Step 1: Write test for activity markers**

Add to `apps/agent-service/tests/unit/test_memory_context.py`:

```python
@pytest.mark.asyncio
async def test_build_people_gestalt_with_activity_markers():
    """印象注入时根据 updated_at 添加活跃度标记"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent_imp = MagicMock(
        user_id="u1", impression_text="很有趣的人",
        updated_at=now - timedelta(days=2),
    )
    stale_imp = MagicMock(
        user_id="u2", impression_text="安静的人",
        updated_at=now - timedelta(days=10),
    )
    very_stale_imp = MagicMock(
        user_id="u3", impression_text="热情的人",
        updated_at=now - timedelta(days=20),
    )

    with (
        patch("app.services.memory_context.get_impressions_for_users",
              new_callable=AsyncMock,
              return_value=[recent_imp, stale_imp, very_stale_imp]),
        patch("app.services.memory_context.get_username",
              new_callable=AsyncMock,
              side_effect=["A哥", "B哥", "C哥"]),
    ):
        from app.services.memory_context import _build_people_gestalt
        lines = await _build_people_gestalt("chat_001", ["u1", "u2", "u3"])

    assert len(lines) == 3
    # Recent: no marker
    assert lines[0] == "- A哥：很有趣的人"
    # 10 days: "最近不太活跃"
    assert "最近不太活跃" in lines[1]
    assert "安静的人" in lines[1]
    # 20 days: "好久没见了"
    assert "好久没见了" in lines[2]
    assert "热情的人" in lines[2]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/unit/test_memory_context.py::test_build_people_gestalt_with_activity_markers -v
```

Expected: FAIL — current code doesn't add activity markers.

- [ ] **Step 3: Implement activity markers in `_build_people_gestalt`**

Edit `apps/agent-service/app/services/memory_context.py`. Replace the `_build_people_gestalt` function (lines 119-130):

```python
async def _build_people_gestalt(chat_id: str, user_ids: list[str]) -> list[str]:
    """构建对话者的感觉 gestalt 列表（含活跃度标记）"""
    impressions = await get_impressions_for_users(
        chat_id, user_ids[:MAX_IMPRESSION_USERS]
    )
    if not impressions:
        return []

    now = datetime.now(CST)
    lines = []
    for imp in impressions:
        name = await get_username(imp.user_id) or imp.user_id[:8]
        # 根据 updated_at 添加活跃度标记
        if imp.updated_at:
            days_since = (now - imp.updated_at.replace(tzinfo=CST)).days
            if days_since > 14:
                lines.append(f"- {name}（好久没见了）：{imp.impression_text}")
            elif days_since > 7:
                lines.append(f"- {name}（最近不太活跃）：{imp.impression_text}")
            else:
                lines.append(f"- {name}：{imp.impression_text}")
        else:
            lines.append(f"- {name}：{imp.impression_text}")
    return lines
```

- [ ] **Step 4: Run all memory_context tests**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/unit/test_memory_context.py -v
```

Expected: All tests PASS (including new activity marker test + existing 4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "feat(impression): add activity markers to people gestalt injection

Impressions now show '好久没见了' (>14d) or '最近不太活跃' (>7d)
based on updated_at, giving 赤尾 natural awareness of who's been
around recently vs who hasn't appeared in a while.
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
