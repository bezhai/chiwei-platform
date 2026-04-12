# Langfuse Chat Prompt Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both `_build_agent()` (run/stream) and `extract()` paths handle Langfuse chat prompts correctly, and migrate safety guards to use Agent's built-in prompt handling.

**Architecture:** Add a `compile_to_messages` helper in `prompts.py` that encapsulates text/chat prompt differences. Consumers (`_build_agent`, `extract`) receive `list[BaseMessage]` and prepend to input messages. Safety guards move from manual `prompt.compile()` to passing `prompt_vars` through `extract()`.

**Tech Stack:** Python 3.13, Langfuse SDK 3.14.1, LangChain, LangGraph, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/agent/prompts.py` | Modify | Add `compile_to_messages()` |
| `app/agent/core.py` | Modify | `_build_agent` returns `(agent, messages)` tuple; `run`/`stream` prepend; `extract` uses helper |
| `app/chat/safety.py` | Modify | Guard configs get prompt_id; drop manual compile; remove `get_prompt` import |
| `tests/unit/agent/test_prompts.py` | Create | Tests for `compile_to_messages` |
| `tests/unit/agent/test_core.py` | Modify | Update mocks from `get_langchain_prompt` to `compile_to_messages`; add chat prompt cases |
| `tests/unit/chat/test_safety.py` | Modify | Update post-check test mock to match new guard pattern |

---

### Task 1: `compile_to_messages` helper + tests

**Files:**
- Modify: `apps/agent-service/app/agent/prompts.py:1-64`
- Create: `apps/agent-service/tests/unit/agent/test_prompts.py`

- [ ] **Step 1: Create test file with text prompt test**

Create `tests/unit/agent/test_prompts.py`:

```python
"""Tests for app.agent.prompts — compile_to_messages."""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.prompts import compile_to_messages

pytestmark = pytest.mark.unit


class TestCompileToMessages:
    def test_text_prompt_returns_system_message(self):
        prompt = MagicMock()
        prompt.type = "text"
        prompt.compile.return_value = "You are a helpful assistant."

        result = compile_to_messages(prompt, name="test")

        assert len(result) == 1
        assert isinstance(result[0], SystemMessage)
        assert result[0].content == "You are a helpful assistant."
        prompt.compile.assert_called_once_with(name="test")

    def test_chat_prompt_maps_roles(self):
        prompt = MagicMock()
        prompt.type = "chat"
        prompt.compile.return_value = [
            {"role": "system", "content": "You are a guard."},
            {"role": "user", "content": "Check: hello"},
        ]

        result = compile_to_messages(prompt, message="hello")

        assert len(result) == 2
        assert isinstance(result[0], SystemMessage)
        assert isinstance(result[1], HumanMessage)
        assert result[0].content == "You are a guard."
        assert result[1].content == "Check: hello"

    def test_chat_prompt_assistant_role(self):
        prompt = MagicMock()
        prompt.type = "chat"
        prompt.compile.return_value = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "example response"},
            {"role": "user", "content": "now your turn"},
        ]

        result = compile_to_messages(prompt)

        assert len(result) == 3
        assert isinstance(result[1], AIMessage)
        assert result[1].content == "example response"

    def test_chat_prompt_unknown_role_defaults_to_system(self):
        prompt = MagicMock()
        prompt.type = "chat"
        prompt.compile.return_value = [
            {"role": "unknown_role", "content": "some content"},
        ]

        result = compile_to_messages(prompt)

        assert len(result) == 1
        assert isinstance(result[0], SystemMessage)

    def test_chat_prompt_missing_content_defaults_to_empty(self):
        prompt = MagicMock()
        prompt.type = "chat"
        prompt.compile.return_value = [
            {"role": "system"},
        ]

        result = compile_to_messages(prompt)

        assert result[0].content == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/test_prompts.py -v`
Expected: FAIL — `ImportError: cannot import name 'compile_to_messages'`

- [ ] **Step 3: Implement `compile_to_messages` in `prompts.py`**

Add imports and function at the end of `app/agent/prompts.py`:

```python
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

_ROLE_TO_MESSAGE: dict[str, type[BaseMessage]] = {
    "system": SystemMessage,
    "user": HumanMessage,
    "assistant": AIMessage,
}


def compile_to_messages(prompt: Any, **variables: Any) -> list[BaseMessage]:
    """Compile a Langfuse prompt into LangChain messages.

    Text prompts become a single SystemMessage.
    Chat prompts become a list of typed messages matching each role.
    """
    if prompt.type == "text":
        return [SystemMessage(content=prompt.compile(**variables))]
    return [
        _ROLE_TO_MESSAGE.get(m.get("role", ""), SystemMessage)(
            content=m.get("content", "")
        )
        for m in prompt.compile(**variables)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/test_prompts.py -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/agent/prompts.py apps/agent-service/tests/unit/agent/test_prompts.py
git commit -m "feat(agent): add compile_to_messages for text/chat prompt unification"
```

---

### Task 2: Rewire `_build_agent`, `run`, `stream` in `core.py`

**Files:**
- Modify: `apps/agent-service/app/agent/core.py:23-227`
- Modify: `apps/agent-service/tests/unit/agent/test_core.py:44-51,78-85,117-191,198-261`

- [ ] **Step 1: Update `mock_prompt` fixture and `mock_deps` in test_core.py**

The `mock_prompt` fixture (line 44) currently mocks `get_langchain_prompt`. Change it to mock `compile` + `type` for `compile_to_messages`:

```python
@pytest.fixture()
def mock_prompt():
    """Mock Langfuse prompt."""
    prompt = MagicMock()
    prompt.type = "text"
    prompt.compile.return_value = "You are a helpful assistant."
    return prompt
```

In `mock_deps` fixture (line 54), replace the `create_agent` patch to also patch `compile_to_messages`:

```python
@pytest.fixture()
def mock_deps(fake_agent, mock_prompt):
    """Patch build_chat_model, get_prompt, create_agent, compile_to_messages, and CallbackHandler."""
    mock_model = AsyncMock()
    mock_model.with_structured_output = MagicMock()

    with (
        patch(
            "app.agent.core.build_chat_model",
            new_callable=AsyncMock,
            return_value=mock_model,
        ) as mock_build,
        patch(
            "app.agent.core.get_prompt",
            return_value=mock_prompt,
        ) as mock_get_prompt,
        patch(
            "app.agent.core.create_agent",
            return_value=fake_agent,
        ) as mock_create,
        patch(
            "app.agent.core.compile_to_messages",
            return_value=[SystemMessage(content="You are a helpful assistant.")],
        ) as mock_compile,
        patch(
            "app.agent.core.CallbackHandler",
            return_value=MagicMock(),
        ),
    ):
        yield {
            "build_chat_model": mock_build,
            "get_prompt": mock_get_prompt,
            "create_agent": mock_create,
            "compile_to_messages": mock_compile,
            "agent": fake_agent,
            "model": mock_model,
            "prompt": mock_prompt,
        }
```

Add `SystemMessage` to the import line at the top:

```python
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
```

- [ ] **Step 2: Update `test_compiles_prompt_via_langfuse` test**

Replace the test at line 140:

```python
    async def test_compiles_prompt_via_langfuse(self, mock_deps):
        await Agent(_CFG).run(
            messages=[HumanMessage(content="hi")],
            prompt_vars={"key": "val"},
        )
        mock_deps["compile_to_messages"].assert_called_once()
        call_kwargs = mock_deps["compile_to_messages"].call_args.kwargs
        assert call_kwargs["key"] == "val"
        assert "currDate" in call_kwargs
        assert "currTime" in call_kwargs
```

- [ ] **Step 3: Add test that `_build_agent` no longer passes `system_prompt`**

Add to `TestRun`:

```python
    async def test_create_agent_called_without_system_prompt(self, mock_deps):
        await Agent(_CFG).run(messages=[HumanMessage(content="hi")])

        call_kwargs = mock_deps["create_agent"].call_args.kwargs
        assert "system_prompt" not in call_kwargs or call_kwargs.get("system_prompt") is None
```

- [ ] **Step 4: Add test that prompt messages are prepended to input**

Add to `TestRun`:

```python
    async def test_prompt_messages_prepended(self, mock_deps):
        mock_deps["compile_to_messages"].return_value = [
            SystemMessage(content="sys prompt"),
        ]
        user_msg = HumanMessage(content="hi")
        await Agent(_CFG).run(messages=[user_msg])

        invoke_args = mock_deps["agent"].ainvoke.call_args[0][0]
        msgs = invoke_args["messages"]
        assert isinstance(msgs[0], SystemMessage)
        assert msgs[0].content == "sys prompt"
        assert msgs[-1] is user_msg
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/test_core.py -v`
Expected: FAIL — `compile_to_messages` not imported in `core.py`

- [ ] **Step 6: Implement changes in `core.py`**

Update imports (line 51):

```python
from app.agent.prompts import compile_to_messages, get_prompt
```

Replace `_build_agent` method (lines 123-142):

```python
    async def _build_agent(
        self, prompt_vars: dict[str, Any]
    ) -> tuple[Any, list[BaseMessage]]:
        """Create a LangGraph agent and compile prompt messages."""
        if not self._cfg.prompt_id:
            raise ValueError(
                f"Agent({self._cfg.trace_name}).run/stream requires a non-empty "
                f"prompt_id. Guard agents (empty prompt_id) should use extract()."
            )
        langfuse_prompt = get_prompt(self._cfg.prompt_id)
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        prompt_messages = compile_to_messages(
            langfuse_prompt,
            currDate=datetime.now().strftime("%Y-%m-%d"),
            currTime=datetime.now().strftime("%H:%M:%S"),
            **prompt_vars,
        )
        agent = create_agent(
            model,
            self._tools,
            context_schema=AgentContext,
        )
        return agent, prompt_messages
```

Update `run` method — change line 167 and add message prepend (lines 158-182):

```python
    async def run(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AIMessage:
        """Execute and return the final ``AIMessage``."""
        agent, prompt_messages = await self._build_agent(prompt_vars or {})
        full_messages = [*prompt_messages, *messages]
        config = self._build_config()

        async def _invoke(msgs: Any, *, config: Any) -> AIMessage:
            result = await agent.ainvoke(
                {"messages": msgs}, context=context, config=config
            )
            return result["messages"][-1]

        return await _retry(
            _invoke,
            full_messages,
            config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).run",
        )
```

Update `stream` method (lines 184-227):

```python
    async def stream(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AsyncGenerator[AIMessageChunk | ToolMessage, None]:
        """Stream tokens."""
        agent, prompt_messages = await self._build_agent(prompt_vars or {})
        full_messages = [*prompt_messages, *messages]
        config = self._build_config()

        for attempt in range(1, max_retries + 1):
            tokens_yielded = False
            try:
                async for token, _ in agent.astream(
                    {"messages": full_messages},
                    context=context,
                    stream_mode="messages",
                    config=config,
                ):
                    tokens_yielded = True
                    yield token
                return
            except RETRYABLE_EXCEPTIONS as e:
                if tokens_yielded:
                    raise
                if attempt < max_retries:
                    delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                    logger.warning(
                        "Agent(%s).stream() attempt %d/%d failed: %s, retrying in %ds",
                        self._cfg.trace_name,
                        attempt,
                        max_retries,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/test_core.py -v`
Expected: all tests PASS

- [ ] **Step 8: Commit**

```bash
git add apps/agent-service/app/agent/core.py apps/agent-service/tests/unit/agent/test_core.py
git commit -m "feat(agent): rewire _build_agent/run/stream to use compile_to_messages"
```

---

### Task 3: Rewire `extract()` in `core.py`

**Files:**
- Modify: `apps/agent-service/app/agent/core.py:229-257`
- Modify: `apps/agent-service/tests/unit/agent/test_core.py:322-396`

- [ ] **Step 1: Add test for extract with chat prompt**

Add to `TestExtract` in `test_core.py`:

```python
    async def test_extract_with_chat_prompt_messages(self, mock_deps):
        """Chat prompt should produce multiple messages prepended to input."""

        class Out(BaseModel):
            v: str

        mock_deps["compile_to_messages"].return_value = [
            SystemMessage(content="You are a guard."),
            HumanMessage(content="Check: test input"),
        ]

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=Out(v="ok"))
        mock_deps["model"].with_structured_output.return_value = structured

        await Agent(_EXTRACT_CFG).extract(Out, messages=[])

        invoke_args = structured.ainvoke.call_args[0][0]
        assert len(invoke_args) == 2
        assert isinstance(invoke_args[0], SystemMessage)
        assert isinstance(invoke_args[1], HumanMessage)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/test_core.py::TestExtract::test_extract_with_chat_prompt_messages -v`
Expected: FAIL — extract still uses old `SystemMessage(content=compile_result)` wrapping

- [ ] **Step 3: Update `extract()` in `core.py`**

Replace the extract method (lines 229-257):

```python
    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> BaseModel:
        """Structured output — return a Pydantic model instance."""
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        structured = model.with_structured_output(response_model)

        prompt_id = self._cfg.prompt_id
        if prompt_id:
            langfuse_prompt = get_prompt(prompt_id)
            prompt_messages = compile_to_messages(
                langfuse_prompt, **(prompt_vars or {})
            )
            messages = [*prompt_messages, *messages]

        config = self._build_config()
        return await _retry(
            structured.ainvoke,
            messages,
            config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).extract",
        )
```

- [ ] **Step 4: Run all core tests**

Run: `cd apps/agent-service && uv run pytest tests/unit/agent/test_core.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/agent/core.py apps/agent-service/tests/unit/agent/test_core.py
git commit -m "feat(agent): rewire extract() to use compile_to_messages"
```

---

### Task 4: Migrate safety guards

**Files:**
- Modify: `apps/agent-service/app/chat/safety.py:26-33,126-194,266-271`
- Modify: `apps/agent-service/tests/unit/chat/test_safety.py:163-175`

- [ ] **Step 1: Update guard AgentConfig constants in safety.py**

Replace lines 26-33:

```python
_GUARD_INJECTION = AgentConfig(
    "guard_prompt_injection", "guard-model", "pre-injection-check"
)
_GUARD_POLITICS = AgentConfig(
    "guard_sensitive_politics", "guard-model", "pre-politics-check"
)
_GUARD_NSFW = AgentConfig("guard_nsfw_content", "guard-model", "pre-nsfw-check")
_GUARD_OUTPUT = AgentConfig("guard_output_safety", "guard-model", "post-safety-check")
```

- [ ] **Step 2: Simplify `_check_injection`**

Replace lines 126-144:

```python
async def _check_injection(message: str) -> PreCheckResult:
    try:
        result: _InjectionResult = await Agent(
            _GUARD_INJECTION, model_kwargs={"reasoning_effort": "low"}
        ).extract(_InjectionResult, messages=[], prompt_vars={"message": message})
        if result.is_injection and result.confidence >= 0.85:
            logger.warning(
                "Prompt injection detected: confidence=%.2f", result.confidence
            )
            return PreCheckResult(
                is_blocked=True,
                block_reason=BlockReason.PROMPT_INJECTION,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Injection check failed: %s", e)
    return PreCheckResult()
```

- [ ] **Step 3: Simplify `_check_politics`**

Replace lines 147-165:

```python
async def _check_politics(message: str) -> PreCheckResult:
    try:
        result: _PoliticsResult = await Agent(
            _GUARD_POLITICS, model_kwargs={"reasoning_effort": "low"}
        ).extract(_PoliticsResult, messages=[], prompt_vars={"message": message})
        if result.is_sensitive and result.confidence >= 0.85:
            logger.warning(
                "Sensitive politics detected: confidence=%.2f", result.confidence
            )
            return PreCheckResult(
                is_blocked=True,
                block_reason=BlockReason.SENSITIVE_POLITICS,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Politics check failed: %s", e)
    return PreCheckResult()
```

- [ ] **Step 4: Simplify `_check_nsfw`**

Replace lines 168-194:

```python
async def _check_nsfw(message: str, persona_id: str) -> PreCheckResult:
    try:
        result: _NsfwResult = await Agent(
            _GUARD_NSFW, model_kwargs={"reasoning_effort": "low"}
        ).extract(_NsfwResult, messages=[], prompt_vars={"message": message})
        if result.is_nsfw and result.confidence >= 0.75:
            if persona_id in _NSFW_BLOCKED_PERSONAS:
                logger.warning(
                    "NSFW blocked: persona=%s, confidence=%.2f",
                    persona_id,
                    result.confidence,
                )
                return PreCheckResult(
                    is_blocked=True,
                    block_reason=BlockReason.NSFW_CONTENT,
                    detail=f"confidence={result.confidence}",
                )
            logger.info(
                "NSFW logged (pass): persona=%s, confidence=%.2f",
                persona_id,
                result.confidence,
            )
    except Exception as e:
        logger.error("NSFW check failed: %s", e)
    return PreCheckResult()
```

- [ ] **Step 5: Simplify output safety in `run_post_check`**

Replace lines 266-271:

```python
        result: _OutputSafetyResult = await Agent(
            _GUARD_OUTPUT, model_kwargs={"reasoning_effort": "low"}
        ).extract(
            _OutputSafetyResult, messages=[], prompt_vars={"response": response_text}
        )
```

- [ ] **Step 6: Remove `get_prompt` import**

Remove `get_prompt` from the import on line 26:

```python
from app.agent.core import Agent, AgentConfig
```

(Delete the `from app.agent.prompts import get_prompt` line entirely.)

- [ ] **Step 7: Verify existing safety tests still pass**

The `test_post_check_clean_text` test patches `Agent` at the class level, so guard configs having prompt_id doesn't affect it — no test changes needed.

- [ ] **Step 8: Run all safety tests**

Run: `cd apps/agent-service && uv run pytest tests/unit/chat/test_safety.py -v`
Expected: all tests PASS

- [ ] **Step 9: Run full test suite**

Run: `cd apps/agent-service && uv run pytest tests/unit/ -v`
Expected: all tests PASS

- [ ] **Step 10: Commit**

```bash
git add apps/agent-service/app/chat/safety.py apps/agent-service/tests/unit/chat/test_safety.py
git commit -m "refactor(safety): migrate guards to use Agent built-in prompt handling"
```

---

### Task 5: Final verification

**Files:** None (read-only checks)

- [ ] **Step 1: Verify no direct `prompt.compile()` in Agent paths**

Run: `grep -rn "\.compile(" apps/agent-service/app/agent/`
Expected: only `compile_to_messages` calling `.compile()` in `prompts.py`

- [ ] **Step 2: Verify `get_prompt` removed from safety.py**

Run: `grep -n "get_prompt" apps/agent-service/app/chat/safety.py`
Expected: no matches

- [ ] **Step 3: Verify `get_langchain_prompt` no longer used anywhere**

Run: `grep -rn "get_langchain_prompt" apps/agent-service/app/`
Expected: no matches

- [ ] **Step 4: Run full test suite one more time**

Run: `cd apps/agent-service && uv run pytest tests/unit/ -v`
Expected: all PASS
