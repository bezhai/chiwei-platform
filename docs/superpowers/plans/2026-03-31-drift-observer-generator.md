# 漂移观察-生成两阶段管线 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将漂移系统从单次 LLM 调用重构为观察→生成两阶段管线，引入自纠正能力

**Architecture:** `_run_drift()` 内部拆为两次串行 LLM 调用：观察 agent 诊断情感状态和回复偏差，生成 agent 根据诊断产出 reply_style。新增 `_get_recent_akao_replies()` 获取赤尾近期回复。Langfuse 新建 `drift_observer` 和 `drift_generator` 两个 prompt 替代 `identity_drift`。

**Tech Stack:** Python / LangChain / Langfuse / Redis

---

### Task 1: 新增 `_get_recent_akao_replies()`

**Files:**
- Modify: `apps/agent-service/app/services/identity_drift.py`
- Test: `apps/agent-service/tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_get_recent_akao_replies_filters_assistant_only():
    """只返回赤尾的回复，不含其他人的消息"""
    mock_messages = [
        MagicMock(role="user", content='{"text":"你好"}', create_time=1000),
        MagicMock(role="assistant", content='{"text":"你好呀～"}', create_time=2000),
        MagicMock(role="user", content='{"text":"在干嘛"}', create_time=3000),
        MagicMock(role="assistant", content='{"text":"发呆"}', create_time=4000),
        MagicMock(role="assistant", content='{"text":"不想动"}', create_time=5000),
    ]

    with (
        patch("app.services.identity_drift.get_chat_messages_in_range",
              new_callable=AsyncMock, return_value=mock_messages),
        patch("app.services.identity_drift.parse_content") as mock_parse,
    ):
        mock_render = MagicMock()
        mock_render.render.side_effect = ["你好呀～", "发呆", "不想动"]
        mock_parse.return_value = mock_render

        from app.services.identity_drift import _get_recent_akao_replies
        result = await _get_recent_akao_replies("chat_001")

    assert "1. " in result
    assert "你好呀～" in result or "发呆" in result
    # 不应该包含 user 消息
    assert "你好" not in result or "你好呀" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py::test_get_recent_akao_replies_filters_assistant_only -v`
Expected: FAIL — `_get_recent_akao_replies` 不存在

- [ ] **Step 3: 实现 `_get_recent_akao_replies`**

在 `identity_drift.py` 末尾，`_get_schedule_context()` 之后添加：

```python
async def _get_recent_akao_replies(chat_id: str, max_replies: int = 10) -> str:
    """获取赤尾最近的回复原文，用于偏差诊断"""
    now = datetime.now(CST)
    # 取最近 2 小时的消息，从中过滤赤尾的回复
    start_ts = int((now - timedelta(hours=2)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return ""

    # 只取赤尾的回复
    akao_msgs = [m for m in messages if m.role == "assistant"]
    akao_msgs = akao_msgs[-max_replies:]

    lines = []
    for i, msg in enumerate(akao_msgs, 1):
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"{i}. {rendered[:200]}")

    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py::test_get_recent_akao_replies_filters_assistant_only -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "feat(drift): 新增 _get_recent_akao_replies 获取赤尾近期回复"
```

---

### Task 2: 重构 `_run_drift()` 为两阶段调用

**Files:**
- Modify: `apps/agent-service/app/services/identity_drift.py`
- Test: `apps/agent-service/tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_run_drift_calls_observer_then_generator():
    """_run_drift 先调 observer 再调 generator，保存 generator 的输出"""
    observer_response = MagicMock()
    observer_response.content = "## 情感状态\n精力低\n## 偏差诊断\n回复太长\n## 下一轮方向\n要短"

    generator_response = MagicMock()
    generator_response.content = "[精力低，懒]\n\n--- 被问问题 ---\n不知道诶\n懒得查"

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(side_effect=[observer_response, generator_response])

    mock_redis = AsyncMock()
    mock_redis.hget = AsyncMock(return_value="上一轮的 reply_style")
    mock_pipe = MagicMock()
    mock_pipe.hset = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.ModelBuilder") as mock_mb,
        patch("app.services.identity_drift.get_prompt") as mock_get_prompt,
        patch("app.services.identity_drift._get_recent_messages",
              new_callable=AsyncMock, return_value="[15:30] A: 你好\n[15:31] 赤尾: 嗯"),
        patch("app.services.identity_drift._get_recent_akao_replies",
              new_callable=AsyncMock, return_value="1. 嗯\n2. 不知道"),
        patch("app.services.identity_drift._get_schedule_context",
              new_callable=AsyncMock, return_value="下午犯困"),
    ):
        mock_redis_cls.get_instance.return_value = mock_redis
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        # 两个不同的 prompt
        mock_observer_prompt = MagicMock()
        mock_observer_prompt.compile.return_value = "observer compiled"
        mock_generator_prompt = MagicMock()
        mock_generator_prompt.compile.return_value = "generator compiled"
        mock_get_prompt.side_effect = lambda name: (
            mock_observer_prompt if name == "drift_observer" else mock_generator_prompt
        )

        from app.services.identity_drift import _run_drift
        await _run_drift("chat_001")

    # 两次 LLM 调用
    assert mock_model.ainvoke.call_count == 2
    # get_prompt 调了 observer 和 generator
    mock_get_prompt.assert_any_call("drift_observer")
    mock_get_prompt.assert_any_call("drift_generator")
    # 保存的是 generator 的输出
    call_args = mock_pipe.hset.call_args
    mapping = call_args.kwargs.get("mapping") or call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["mapping"]
    assert "精力低" in mapping["state"] or "懒" in mapping["state"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py::test_run_drift_calls_observer_then_generator -v`
Expected: FAIL — 还在调用 `identity_drift` prompt

- [ ] **Step 3: 重写 `_run_drift()`**

将 `identity_drift.py` 中的 `_run_drift` 函数替换为：

```python
async def _run_drift(chat_id: str) -> None:
    """两阶段漂移管线：观察 → 生成

    Agent 1（观察）：群聊事件 + 赤尾近期回复 + 基准人设 → 观察报告
    Agent 2（生成）：观察报告 → reply_style
    """
    # 1. 收集上下文
    current_state = await get_identity_state(chat_id)
    recent_messages = await _get_recent_messages(chat_id)
    schedule_context = await _get_schedule_context()
    recent_replies = await _get_recent_akao_replies(chat_id)

    if not recent_messages:
        logger.info(f"No recent messages for {chat_id}, skip drift")
        return

    now = datetime.now(CST)
    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)

    # 2. Agent 1: 观察
    observer_prompt = get_prompt("drift_observer")
    observer_compiled = observer_prompt.compile(
        schedule_daily=schedule_context,
        current_reply_style=current_state or "（刚醒来，还没有形成今天的说话方式）",
        message_buffer=recent_messages,
        recent_akao_replies=recent_replies or "（还没有最近的回复）",
        current_time=now.strftime("%H:%M"),
    )

    observer_response = await model.ainvoke(
        [{"role": "user", "content": observer_compiled}],
    )
    observation_report = _extract_text(observer_response.content)

    if not observation_report:
        logger.warning(f"Observer returned empty for {chat_id}")
        return

    logger.info(f"Drift observer for {chat_id}: {observation_report[:80]}...")

    # 3. Agent 2: 生成
    generator_prompt = get_prompt("drift_generator")
    generator_compiled = generator_prompt.compile(
        observation_report=observation_report,
    )

    generator_response = await model.ainvoke(
        [{"role": "user", "content": generator_compiled}],
    )
    new_style = _extract_text(generator_response.content)

    if not new_style:
        logger.warning(f"Generator returned empty for {chat_id}")
        return

    # 4. 保存
    await set_identity_state(chat_id, new_style)


def _extract_text(content) -> str:
    """从 LLM response content 提取纯文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py::test_run_drift_calls_observer_then_generator -v`
Expected: PASS

- [ ] **Step 5: 运行全部现有测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py -v`
Expected: 新测试 PASS，旧的 `test_run_drift_calls_llm_and_saves_state` 会 FAIL（因为 prompt name 变了）

- [ ] **Step 6: 更新旧测试**

将 `test_run_drift_calls_llm_and_saves_state` 删除（已被 `test_run_drift_calls_observer_then_generator` 替代）。同时更新 `test_get_identity_state_returns_none_when_empty` 和 `test_set_and_get_identity_state` 中的 Redis key 从 `"identity:chat_001"` 改为 `"reply_style:chat_001"`。

- [ ] **Step 7: 全部测试通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py -v`
Expected: ALL PASS

- [ ] **Step 8: 提交**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "refactor(drift): _run_drift 拆为观察→生成两阶段管线"
```

---

### Task 3: 创建 Langfuse prompts

**Files:** 无代码文件，纯 Langfuse 操作

- [ ] **Step 1: 创建 `drift_observer` prompt**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "drift_observer",
  "type": "text",
  "prompt": "你是赤尾的\"行为观察员\"。你的任务是观察赤尾最近的对话和回复，诊断她的情感状态和回复偏差。\n\n赤尾的性格基准（她\"正常\"的样子）：\n元气活泼是底色。腹黑藏得深——笑嘻嘻但心里看得透。对在意的人有说不出口的占有欲。\n傲娇是保护色，好奇心是驱动力。善良但不讨好，累了会敷衍，烦了会直接拒绝。\n说话像真人发微信：短、随意，语气词是武器。默认回复在 15 字以内。\n表情优先用颜文字，种类丰富不重复，偶尔 emoji。\n遇到不感兴趣或不懂的话题会敷衍（\"不知道诶\"\"你自己查啦\"），不会认真科普。\n\n赤尾今天的日程：\n{{schedule_daily}}\n\n赤尾上一轮的说话方式：\n{{current_reply_style}}\n\n刚才群里发生的事：\n{{message_buffer}}\n\n赤尾最近的回复原文：\n{{recent_akao_replies}}\n\n---\n\n现在是 {{current_time}}。\n\n请输出观察报告，包含三部分：\n\n## 情感状态\n用一句直白的话描述此刻的精力和心情（\"精力低，懒得动\"而不是\"像裹在云里\"）\n\n## 偏差诊断\n对比赤尾的性格基准，看她最近的回复有什么偏差。关注：\n- 长度：是否偏长？状态是\"懒\"时不该展开回答\n- 口癖：颜文字、语气词、标点是否重复太多？列出具体重复项\n- 角色：有没有像 AI 助手一样认真回答知识类问题？\n- 多样性：回复结构是否雷同？（比如每条都是\"句子 + 颜文字\"）\n如果没有明显偏差，写\"暂无明显偏差\"\n\n## 下一轮方向\n根据情感状态和偏差诊断，给出下一轮回复示例的生成方向",
  "labels": ["perf-context-optimize"]
}'
```

- [ ] **Step 2: 创建 `drift_generator` prompt**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "drift_generator",
  "type": "text",
  "prompt": "你是赤尾的\"说话方式生成器\"。根据观察报告，生成赤尾此刻的回复示例。\n\n这些示例会直接注入另一个模型的 system prompt，作为赤尾回复的行为锚点。\n\n观察报告：\n{{observation_report}}\n\n---\n\n根据观察报告中的情感状态和纠偏方向，生成赤尾此刻的回复示例。\n\n要求：\n- 先写一句话状态概括\n- 然后写 4-5 个此刻最可能遇到的场景的回复示例\n- 示例长度必须和情感状态匹配：状态懒就短（10 字以内），状态高就可以长一些\n- 如果偏差诊断指出了问题（如颜文字重复、回复太长），示例中必须体现纠正\n- 表情优先用颜文字，种类丰富不重复\n- 这是行为锚点，不是文学创作\n\n格式：\n\n[一句话状态]\n\n--- 场景描述 ---\n示例回复\n另一条示例\n\n--- 另一个场景 ---\n示例回复",
  "labels": ["perf-context-optimize"]
}'
```

- [ ] **Step 3: 验证 prompt 创建成功**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py get-prompt '{"name":"drift_observer","label":"perf-context-optimize"}'
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py get-prompt '{"name":"drift_generator","label":"perf-context-optimize"}'
```

- [ ] **Step 4: 提交记录**

无代码改动，在 commit message 中记录 prompt 创建：

```bash
git commit --allow-empty -m "feat(langfuse): 创建 drift_observer 和 drift_generator prompts"
```

---

### Task 4: 部署验证

**Files:** 无新改动

- [ ] **Step 1: 推送代码**

```bash
git push origin perf/context-optimize
```

- [ ] **Step 2: 部署到泳道**

```bash
make deploy APP=agent-service LANE=perf-context-optimize GIT_REF=perf/context-optimize
```

- [ ] **Step 3: 验证漂移日志**

等群里有消息触发漂移后，检查日志：

```bash
make logs APP=agent-service LANE=perf-context-optimize KEYWORD=observer SINCE=5m
```

应该看到：`Drift observer for oc_xxx: ## 情感状态...`

- [ ] **Step 4: 验证 trace 中的 reply-style**

从 Langfuse 拉最新 trace，检查 `<reply-style>` 区域是否包含 generator 输出的行为示例。

- [ ] **Step 5: 观察回复质量**

拉最近 10 条回复，检查：
- 回复长度是否与漂移状态匹配
- 颜文字/口癖是否多样化
- 知识类问题是否不再认真科普
