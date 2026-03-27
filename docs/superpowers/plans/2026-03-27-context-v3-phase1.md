# Context System v3 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optimize 赤尾's reply style, fuzzy-ify history search output, and make impression evolution natural — all through minimal code changes (mostly Langfuse prompts + two Python files).

**Architecture:** Three independent changes: (D) Langfuse `main` prompt rewrite for reply brevity/refusal; (C) `search_group_history` tool description + output format change; (B) `post_process_impressions` prompt rewrite + `_build_people_gestalt` activity marker.

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

### Task 2: C-Phase1 — search_group_history 模糊化

**Files:**
- Modify: `apps/agent-service/app/agents/tools/history/search.py`
- Test: `apps/agent-service/tests/unit/test_search_history_fuzzy.py`

- [ ] **Step 1: Write tests for fuzzy timestamp and truncation**

Create `apps/agent-service/tests/unit/test_search_history_fuzzy.py`:

```python
"""search_group_history 模糊化输出测试"""

from app.agents.tools.history.search import _format_timestamp_fuzzy, _truncate


def test_format_timestamp_fuzzy_today():
    """今天的消息显示'今天'"""
    from datetime import datetime
    now = datetime.now()
    ts = int(now.timestamp() * 1000)
    result = _format_timestamp_fuzzy(ts)
    assert result == "今天"


def test_format_timestamp_fuzzy_yesterday():
    """昨天的消息显示'昨天'"""
    from datetime import datetime, timedelta
    yesterday = datetime.now() - timedelta(days=1)
    ts = int(yesterday.timestamp() * 1000)
    result = _format_timestamp_fuzzy(ts)
    assert result == "昨天"


def test_format_timestamp_fuzzy_days_ago():
    """3天前显示'几天前'"""
    from datetime import datetime, timedelta
    three_days_ago = datetime.now() - timedelta(days=3)
    ts = int(three_days_ago.timestamp() * 1000)
    result = _format_timestamp_fuzzy(ts)
    assert result == "几天前"


def test_format_timestamp_fuzzy_week_ago():
    """8天前显示'上周'"""
    from datetime import datetime, timedelta
    week_ago = datetime.now() - timedelta(days=8)
    ts = int(week_ago.timestamp() * 1000)
    result = _format_timestamp_fuzzy(ts)
    assert result == "上周"


def test_format_timestamp_fuzzy_long_ago():
    """30天前显示'很久以前'"""
    from datetime import datetime, timedelta
    long_ago = datetime.now() - timedelta(days=30)
    ts = int(long_ago.timestamp() * 1000)
    result = _format_timestamp_fuzzy(ts)
    assert result == "很久以前"


def test_truncate_default_80():
    """默认截断80字"""
    long_text = "a" * 100
    result = _truncate(long_text)
    assert len(result) == 83  # 80 + "..."
    assert result.endswith("...")


def test_truncate_short_text():
    """短文本不截断"""
    result = _truncate("hello")
    assert result == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/unit/test_search_history_fuzzy.py -v
```

Expected: FAIL — `_format_timestamp_fuzzy` does not exist yet.

- [ ] **Step 3: Implement fuzzy timestamp + update truncation + update tool description**

Edit `apps/agent-service/app/agents/tools/history/search.py`:

Replace `_format_timestamp` with `_format_timestamp_fuzzy`:

```python
def _format_timestamp_fuzzy(ts: int) -> str:
    """将精确时间戳转为模糊时间描述"""
    now = datetime.now()
    msg_time = datetime.fromtimestamp(ts / 1000)
    delta = now - msg_time
    days = delta.days

    if days == 0:
        return "今天"
    elif days == 1:
        return "昨天"
    elif days < 7:
        return "几天前"
    elif days < 14:
        return "上周"
    else:
        return "很久以前"
```

Change `_truncate` default from 200 to 80:

```python
def _truncate(text: str, max_len: int = 80) -> str:
```

Update the tool docstring from precise search description to fuzzy recall:

```python
@tool
async def search_group_history(
    query: str,
    limit: int = 5,
) -> str:
    """
    模模糊糊回忆之前群里好像聊过的事

    当你隐约记得群里聊过某个话题但细节模糊了，可以用这个回想一下。
    不要用来确认事实或引用别人的原话——你的记忆本来就是模糊的。

    Args:
        query: 你隐约记得的内容（自然语言描述）
        limit: 最多回忆几个片段（默认5个）

    Returns:
        str: 模糊的回忆片段
    """
```

Update the output formatting section (line ~160-178), replace:

```python
        time_str = _format_timestamp(msg.create_time)
```

with:

```python
        time_str = _format_timestamp_fuzzy(msg.create_time)
```

And change the result header:

```python
        lines = [f"隐约记得有 {len(anchor_set)} 段相关的事：\n"]
```

And remove the `→ ` marker for anchor messages (too precise), replace:

```python
            marker = "→ " if msg.message_id in anchor_set else "  "
            lines.append(f"{marker}[{time_str}] {user.name}: {content}")
```

with:

```python
            lines.append(f"  [{time_str}] {user.name}: {content}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/unit/test_search_history_fuzzy.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Run existing tests to check for regressions**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/docs-review-context-system/apps/agent-service
uv run pytest tests/ -v --timeout=30 2>&1 | tail -20
```

Expected: No new failures.

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/agents/tools/history/search.py apps/agent-service/tests/unit/test_search_history_fuzzy.py
git commit -m "feat(search): fuzzy-ify search_group_history output

- Replace precise timestamps with fuzzy time ('今天', '几天前', '上周')
- Reduce content truncation from 200 to 80 chars
- Reduce default limit from 10 to 5
- Rewrite tool description to discourage precise recall
- Remove anchor markers from output
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
