# Langfuse Chat Prompt Support

## Problem

Agent-service has two prompt consumption paths that only handle text prompts correctly.
Chat prompts (multi-message `[{role, content}]` structure) break silently:

| Path | Text Prompt | Chat Prompt |
|------|-------------|-------------|
| `_build_agent()` → `get_langchain_prompt()` → `create_agent(system_prompt=...)` | `str` — OK | `List[Tuple]` — **breaks** (`create_agent` expects `str \| SystemMessage`) |
| `extract()` → `.compile()` → `SystemMessage(content=...)` | `str` — OK | `list[dict]` — **breaks** (list stuffed into SystemMessage content) |

Safety guards work by accident: they call `.compile()` directly and pass results as `messages=` to `extract()`,
bypassing both broken paths because their `prompt_id=""`.

Currently 5 of 34 Langfuse prompts are chat type (4 guards + `pre_complexity_classification`).

## Design

### Core: `compile_to_messages` helper in `prompts.py`

A single function that compiles any Langfuse prompt into `list[BaseMessage]`:

- **Text prompt**: `prompt.compile(**vars)` returns `str` → `[SystemMessage(content=str)]`
- **Chat prompt**: `prompt.compile(**vars)` returns `list[dict]` → each dict mapped to the corresponding LangChain message type via role

```python
_ROLE_TO_MESSAGE = {
    "system": SystemMessage,
    "user": HumanMessage,
    "assistant": AIMessage,
}

def compile_to_messages(prompt, **variables) -> list[BaseMessage]:
    if prompt.type == "text":
        return [SystemMessage(content=prompt.compile(**variables))]
    return [
        _ROLE_TO_MESSAGE.get(m["role"], SystemMessage)(content=m.get("content", ""))
        for m in prompt.compile(**variables)
    ]
```

Consumers see only `list[BaseMessage]`, prompt type differences are fully encapsulated.

### `_build_agent()` — drop `system_prompt`, prepend messages

Before: `create_agent(model, tools, system_prompt=get_langchain_prompt(...))` — only works for text.

After:
1. Call `compile_to_messages(prompt, currDate=..., currTime=..., **prompt_vars)` → `list[BaseMessage]`
2. Call `create_agent(model, tools, context_schema=AgentContext)` — no `system_prompt`
3. Return `(agent, prompt_messages)` tuple

`run()` and `stream()` unpack the tuple and prepend `prompt_messages` to input messages:
```python
agent, prompt_messages = await self._build_agent(prompt_vars or {})
full_messages = [*prompt_messages, *messages]
```

Functionally equivalent: the LangGraph agent sees the same message sequence.

### `extract()` — use helper instead of manual wrapping

Before:
```python
system = get_prompt(prompt_id).compile(**(prompt_vars or {}))
messages = [SystemMessage(content=system), *messages]
```

After:
```python
langfuse_prompt = get_prompt(prompt_id)
prompt_messages = compile_to_messages(langfuse_prompt, **(prompt_vars or {}))
messages = [*prompt_messages, *messages]
```

Chat prompts with multiple roles (system + user) are handled naturally.

### Safety guards — migrate into `extract()`

Guards currently manage prompts externally (compile in safety.py, pass raw dicts as messages).
Migrate them to use Agent's built-in prompt handling:

**AgentConfig changes:**
```python
# Before
_GUARD_INJECTION = AgentConfig("", "guard-model", "guard-injection")

# After
_GUARD_INJECTION = AgentConfig("guard_prompt_injection", "guard-model", "guard-injection")
```

**Call site simplification (4 places):**
```python
# Before
prompt = get_prompt("guard_prompt_injection")
messages = prompt.compile(message=message)
result = await Agent(_GUARD_INJECTION, ...).extract(_InjectionResult, messages=messages)

# After
result = await Agent(_GUARD_INJECTION, ...).extract(
    _InjectionResult,
    messages=[],
    prompt_vars={"message": message},
)
```

`safety.py` no longer imports `get_prompt`. All prompt management is inside Agent.

### Not changed

- **`context_builder`** in `context.py`: calls `get_prompt().compile()` directly, not through Agent. Out of scope.
- **`currDate`/`currTime` auto-injection**: only in `_build_agent`, not in `extract`. Preserved as-is.
- **Lane-aware prompt routing**: handled by `get_prompt()`, untouched.

## Files Changed

| File | Change |
|------|--------|
| `app/agent/prompts.py` | Add `compile_to_messages()` |
| `app/agent/core.py` | `_build_agent` returns `(agent, prompt_messages)`, drops `system_prompt`; `run`/`stream` prepend messages; `extract` uses helper |
| `app/chat/safety.py` | Guard configs get prompt_id; 4 check functions drop manual compile, use `prompt_vars`; remove `get_prompt` import |
| `tests/unit/agent/test_core.py` | Mock `prompt.compile` + `prompt.type` instead of `get_langchain_prompt`; add chat prompt test cases |

## Verification

- Existing text prompt behavior unchanged (unit tests)
- Chat prompt compilation produces correct message types (unit tests)
- Guard prompts work through `extract()` with prompt_id (unit tests)
- `_build_agent` message prepending equivalent to `system_prompt` injection (unit test comparing message sequences)
