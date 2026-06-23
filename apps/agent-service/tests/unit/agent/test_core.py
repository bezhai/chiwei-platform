"""test_core.py -- Agent unified interface tests (post-langchain cutover).

Covers the Agent layer over the neutral ``ModelClient`` (no langchain /
create_agent / CallbackHandler):
  - Agent.run(): returns final neutral Message, retry on transient errors,
    prompt compilation + message threading, recursion_limit passthrough.
  - Agent.stream(): forwards neutral StreamChunks, no retry after first yield.
  - Agent.extract(): structured output validated into a Pydantic model.
  - AgentConfig as frozen dataclass.

The ReAct loop control flow itself is de-risked separately in
``test_react_loop.py`` against a scripted fake ModelClient; here we mock the
loop functions / model client to assert the Agent's own responsibilities.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APITimeoutError
from pydantic import BaseModel

from app.agent.core import Agent, AgentConfig
from app.agent.context import AgentContext
from app.agent.neutral import Message, Role, StreamChunk

pytestmark = pytest.mark.unit

# Reusable test configs
_CFG = AgentConfig("test_prompt", "test-model", "test-agent")
_EXTRACT_CFG = AgentConfig(
    "relationship_filter", "relationship-model", "relationship-filter"
)
_NO_PROMPT_CFG = AgentConfig("", "guard-model", "guard")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_prompt():
    """Mock Langfuse prompt (text prompt)."""
    prompt = MagicMock()
    prompt.type = "text"
    prompt.compile.return_value = "You are a helpful assistant."
    return prompt


@pytest.fixture()
def mock_deps(mock_prompt):
    """Patch build_model_client, get_prompt, compile_to_messages, and the root span.

    The root span is patched to a no-op so tracing/langfuse never reaches the
    network in unit tests. ``compile_to_messages`` returns a neutral SYSTEM
    message by default.
    """
    model = AsyncMock()
    model.complete = AsyncMock(
        return_value=Message(role=Role.ASSISTANT, content="hello")
    )
    model.structured = AsyncMock(return_value={"v": "ok"})

    from contextlib import contextmanager

    @contextmanager
    def _noop_span(**_kwargs):
        yield MagicMock()

    with (
        patch(
            "app.agent.core.build_model_client",
            new_callable=AsyncMock,
            return_value=model,
        ) as mock_build,
        patch(
            "app.agent.core.get_prompt",
            return_value=mock_prompt,
        ) as mock_get_prompt,
        patch(
            "app.agent.core.compile_to_messages",
            return_value=[Message(role=Role.SYSTEM, content="You are a helpful assistant.")],
        ) as mock_compile,
        patch("app.agent.core._root_span", _noop_span),
    ):
        yield {
            "build_model_client": mock_build,
            "get_prompt": mock_get_prompt,
            "compile_to_messages": mock_compile,
            "model": model,
            "prompt": mock_prompt,
        }


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


class TestRootSpanRobustness:
    """Tracing must never break the call — span machinery failures degrade."""

    def test_root_span_yields_even_when_enter_fails(self):
        from app.agent import core

        class _BadCM:
            def __enter__(self):
                raise RuntimeError("otel context enter exploded")

            def __exit__(self, *a):
                return False

        client = MagicMock()
        client.start_as_current_span.return_value = _BadCM()
        with patch.object(core, "_get_trace_client", return_value=client):
            entered = False
            with core._root_span(name="x", input=[], update_trace=True):
                entered = True
            assert entered  # the body ran despite the span enter blowing up

    def test_root_span_does_not_swallow_body_exception(self):
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with pytest.raises(ValueError, match="body boom"):
                with core._root_span(name="x", input=[], update_trace=False):
                    raise ValueError("body boom")

    def test_tool_span_yields_even_when_enter_fails(self):
        from app.agent import core

        class _BadCM:
            def __enter__(self):
                raise RuntimeError("tool span enter exploded")

            def __exit__(self, *a):
                return False

        client = MagicMock()
        client.start_as_current_span.return_value = _BadCM()
        with patch.object(core, "_get_trace_client", return_value=client):
            entered = False
            with core._tool_span(name="t", input={}):
                entered = True
            assert entered

    def test_tool_span_reparents_under_current_generation(self):
        """A tool span attaches to the model call that requested it: the
        generation's TraceContext (parent_span_id) so it nests under that
        generation, not flat under the agent root."""
        from app.agent import core

        fake_ctx = {"trace_id": "a" * 32, "parent_span_id": "b" * 16}
        client = MagicMock()
        with (
            patch.object(core, "_get_trace_client", return_value=client),
            patch.object(core, "current_generation_context", return_value=fake_ctx),
        ):
            with core._tool_span(name="recall", input={}):
                pass
        kwargs = client.start_as_current_span.call_args.kwargs
        assert kwargs.get("trace_context") == fake_ctx

    def test_tool_span_no_reparent_without_generation(self):
        from app.agent import core

        client = MagicMock()
        with (
            patch.object(core, "_get_trace_client", return_value=client),
            patch.object(core, "current_generation_context", return_value=None),
        ):
            with core._tool_span(name="recall", input={}):
                pass
        kwargs = client.start_as_current_span.call_args.kwargs
        assert kwargs.get("trace_context") is None


class TestRootSpanTurnTrace:
    """Root span attaches the active turn's langfuse trace, so the guard
    extracts and the main stream of one (message_id, persona_id) turn land in
    ONE trace instead of separate top-level traces."""

    def test_root_span_attaches_turn_trace_context(self):
        from app.agent import core
        from app.agent.trace import current_turn_trace_id, turn_trace

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with turn_trace("msg-1:persona-2"):
                expected_tid = current_turn_trace_id()
                with core._root_span(name="main", input=[], update_trace=False):
                    pass
        kwargs = client.start_as_current_span.call_args.kwargs
        assert kwargs.get("trace_context") == {"trace_id": expected_tid}

    def test_root_span_no_trace_context_outside_turn(self):
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(name="main", input=[], update_trace=False):
                pass
        kwargs = client.start_as_current_span.call_args.kwargs
        assert kwargs.get("trace_context") is None

    def test_two_root_spans_same_turn_share_trace_id(self):
        """Anti-false-positive (codex T1 必改 3 at the root layer): a guard
        root span and a main root span opened under the SAME turn attach to the
        SAME trace_id — real unification, not each opening its own trace."""
        from app.agent import core
        from app.agent.trace import turn_trace

        client = MagicMock()
        seen = []
        with patch.object(core, "_get_trace_client", return_value=client):
            with turn_trace("msg-5:persona-1"):
                with core._root_span(
                    name="pre-nsfw-check", input=[], update_trace=False
                ):
                    pass
                seen.append(
                    client.start_as_current_span.call_args.kwargs.get("trace_context")
                )
                with core._root_span(name="main", input=[], update_trace=True):
                    pass
                seen.append(
                    client.start_as_current_span.call_args.kwargs.get("trace_context")
                )
        assert seen[0] is not None
        assert seen[0] == seen[1]
        assert seen[0]["trace_id"]


class TestTurnTraceUnifiedName:
    """In a unified turn trace the guards / main / post-safety are separate root
    spans on ONE trace; langfuse names the whole trace after whichever root span
    is ingested LAST (post-safety, since it runs last). Every root span instead
    writes the SAME trace-level name so the trace name is stable. Crucially the
    per-span OBSERVATION names stay distinct — trace name and span name are
    independent langfuse fields, so unifying the trace name must not pollute the
    sub-span names."""

    def test_guard_root_span_keeps_span_name_sets_unified_trace_name(self):
        from app.agent import core
        from app.agent.trace import TURN_TRACE_NAME, turn_trace

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with turn_trace("msg-1:persona-2"):
                with core._root_span(
                    name="pre-nsfw-check", input=[{"x": 1}], update_trace=False
                ):
                    pass
        # the OBSERVATION/span name is preserved — NOT polluted to the trace name
        assert (
            client.start_as_current_span.call_args.kwargs["name"] == "pre-nsfw-check"
        )
        # the TRACE-level name is the unified turn name
        kw = client.update_current_trace.call_args.kwargs
        assert kw["name"] == TURN_TRACE_NAME
        # a guard does NOT own the trace input
        assert kw.get("input") is None

    def test_main_root_span_sets_unified_name_and_owns_input(self):
        from app.agent import core
        from app.agent.trace import TURN_TRACE_NAME, turn_trace

        client = MagicMock()
        chat_input = [{"role": "user", "content": "hi"}]
        with patch.object(core, "_get_trace_client", return_value=client):
            with turn_trace("msg-1:persona-2"):
                with core._root_span(
                    name="main", input=chat_input, update_trace=True
                ):
                    pass
        # span name preserved
        assert client.start_as_current_span.call_args.kwargs["name"] == "main"
        kw = client.update_current_trace.call_args.kwargs
        assert kw["name"] == TURN_TRACE_NAME
        # main owns the trace input (the chat), so the trace top reads the turn
        assert kw["input"] == chat_input

    def test_two_turn_root_spans_write_same_trace_name(self):
        """The fix's invariant: main (runs first) and post-safety (ingested last)
        both write TURN_TRACE_NAME, so order no longer decides the trace name."""
        from app.agent import core
        from app.agent.trace import TURN_TRACE_NAME, turn_trace

        client = MagicMock()
        names: list = []
        client.update_current_trace.side_effect = lambda **kw: names.append(
            kw.get("name")
        )
        with patch.object(core, "_get_trace_client", return_value=client):
            with turn_trace("msg-9:persona-3"):
                with core._root_span(name="main", input=[], update_trace=True):
                    pass
                with core._root_span(
                    name="post-safety-check", input=[], update_trace=False
                ):
                    pass
        assert names == [TURN_TRACE_NAME, TURN_TRACE_NAME]

    def test_outside_turn_keeps_span_name_as_trace_name(self):
        """No turn → unchanged: update_trace=True still names the trace after the
        span (world-engine / post-safety-without-turn become their own
        traces named after themselves)."""
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(name="world-engine", input=[1], update_trace=True):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert kw["name"] == "world-engine"
        assert kw["input"] == [1]

    def test_outside_turn_no_update_when_update_trace_false(self):
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(name="guard", input=[], update_trace=False):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert kw.get("name") is None
        assert kw.get("input") is None
        assert kw["tags"] == ["app:agent-service", "lane:prod", "lane_class:prod"]


class TestRootSpanSession:
    """A run can be bound to a langfuse session so several traces (e.g. a
    persona's whole day of thinking) group together. session_id is a *trace*
    attribute (langfuse ``update_current_trace(session_id=...)``), NOT part of
    ``start_as_current_span``'s ``trace_context``. When None the trace is
    untouched re: session — the chat path passes no session and must behave
    exactly as before."""

    def test_session_id_set_on_trace_when_provided(self):
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(
                name="world-deliberate",
                input=[1],
                update_trace=True,
                session_id="prod:world:2026-06-04",
            ):
                pass
        # session_id reaches langfuse via update_current_trace (the only place
        # session_id can be associated with a trace in the v3 SDK)
        seen = [
            c.kwargs.get("session_id")
            for c in client.update_current_trace.call_args_list
        ]
        assert "prod:world:2026-06-04" in seen

    def test_session_id_set_even_when_update_trace_false(self):
        """A guard span (update_trace=False) on a session-bound run must still
        tag the trace's session — session grouping is orthogonal to who owns the
        trace name/input."""
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(
                name="guard",
                input=[],
                update_trace=False,
                session_id="prod:world:2026-06-04",
            ):
                pass
        seen = [
            c.kwargs.get("session_id")
            for c in client.update_current_trace.call_args_list
        ]
        assert "prod:world:2026-06-04" in seen

    def test_no_session_id_does_not_touch_trace_session(self):
        """Backward compat: without a session_id the run behaves exactly as
        before re: session — no update_current_trace call carries a session_id,
        while lane tags/metadata are still written for trace lookup."""
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(name="guard", input=[], update_trace=False):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert kw.get("session_id") is None
        assert kw["metadata"]["lane"] == "prod"

    def test_no_session_id_with_update_trace_keeps_status_quo(self):
        """update_trace=True without a session: name+input are set and no
        session_id leaks into the call."""
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(name="world-engine", input=[1], update_trace=True):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert kw["name"] == "world-engine"
        assert kw["input"] == [1]
        assert kw.get("session_id") is None


class TestRootSpanLaneAttributes:
    """Lane must be written as trace tags because list-traces filters tags
    directly; metadata mirrors the same fields for trace detail inspection."""

    def test_request_lane_writes_queryable_tags_and_metadata(self):
        from app.agent import core

        client = MagicMock()
        with (
            patch.object(core, "_get_trace_client", return_value=client),
            patch.object(core, "get_lane", return_value="ppe-lf"),
            patch.object(core, "current_deployment_lane", return_value=None),
        ):
            with core._root_span(name="main", input=[], update_trace=True):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert kw["tags"] == [
            "app:agent-service",
            "lane:ppe-lf",
            "lane_class:ppe",
        ]
        assert kw["metadata"] == {
            "app": "agent-service",
            "lane": "ppe-lf",
            "laneClass": "ppe",
            "laneSource": "request",
        }

    def test_deployment_lane_used_when_request_lane_absent(self):
        from app.agent import core

        client = MagicMock()
        with (
            patch.object(core, "_get_trace_client", return_value=client),
            patch.object(core, "get_lane", return_value=None),
            patch.object(core, "current_deployment_lane", return_value="coe-lf"),
        ):
            with core._root_span(name="world", input=[], update_trace=True):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert "lane:coe-lf" in kw["tags"]
        assert "lane_class:coe" in kw["tags"]
        assert kw["metadata"]["laneSource"] == "deployment"

    def test_prod_default_when_no_lane_context(self):
        from app.agent import core

        client = MagicMock()
        with (
            patch.object(core, "_get_trace_client", return_value=client),
            patch.object(core, "get_lane", return_value=None),
            patch.object(core, "current_deployment_lane", return_value=None),
        ):
            with core._root_span(name="main", input=[], update_trace=False):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert "lane:prod" in kw["tags"]
        assert kw["metadata"]["lane"] == "prod"
        assert kw["metadata"]["laneSource"] == "default"


class TestRunSessionPlumbing:
    """The session_id rides on AgentContext (the existing per-run context) so no
    new public parameter is needed; chat passes a context without a session and
    is unaffected, while world/life pass a context carrying their daily session
    id."""

    async def test_run_threads_context_session_id_to_root_span(self, mock_deps):
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None):
            captured["session_id"] = session_id
            yield MagicMock()

        with patch("app.agent.core._root_span", _spy_span):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")],
                context=AgentContext(
                    persona_id="luna", session_id="prod:luna:2026-06-04"
                ),
            )
        assert captured["session_id"] == "prod:luna:2026-06-04"

    async def test_run_without_context_passes_none_session(self, mock_deps):
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None):
            captured["session_id"] = session_id
            yield MagicMock()

        with patch("app.agent.core._root_span", _spy_span):
            await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])
        assert captured["session_id"] is None

    async def test_run_context_without_session_passes_none(self, mock_deps):
        """The chat path: AgentContext built without session_id → no session."""
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None):
            captured["session_id"] = session_id
            yield MagicMock()

        with patch("app.agent.core._root_span", _spy_span):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")],
                context=AgentContext(message_id="m", chat_id="c", persona_id="luna"),
            )
        assert captured["session_id"] is None

    async def test_stream_threads_context_session_id_to_root_span(self, mock_deps):
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None):
            captured["session_id"] = session_id
            yield MagicMock()

        async def fake_stream(messages, *, tools=None, **kwargs):
            yield StreamChunk(text="hi")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = fake_stream
        with patch("app.agent.core._root_span", _spy_span):
            async for _ in Agent(_CFG).stream(
                messages=[Message(role=Role.USER, content="hi")],
                context=AgentContext(session_id="prod:world:2026-06-04"),
            ):
                pass
        assert captured["session_id"] == "prod:world:2026-06-04"


class TestModelCacheSessionId:
    """The trace session_id (run arg or context) is forwarded to model.complete /
    model.stream as the prompt-cache key, so the azure adapter can wire it into
    the gateway's session cache header. None when there is no session (adapter
    no-ops), keeping the chat / stateless path unchanged."""

    async def test_run_forwards_context_session_id_to_model(self, mock_deps):
        await Agent(_CFG).run(
            [Message(role=Role.USER, content="hi")],
            context=AgentContext(session_id="prod:world:2026-06-04"),
        )
        kw = mock_deps["model"].complete.call_args.kwargs
        assert kw.get("session_id") == "prod:world:2026-06-04"

    async def test_run_without_session_forwards_none(self, mock_deps):
        await Agent(_CFG).run([Message(role=Role.USER, content="hi")])
        kw = mock_deps["model"].complete.call_args.kwargs
        assert kw.get("session_id") is None

    async def test_stream_forwards_context_session_id_to_model(self, mock_deps):
        captured: dict = {}

        async def fake_stream(messages, *, tools=None, **kwargs):
            captured.update(kwargs)
            yield StreamChunk(text="hi")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = fake_stream
        async for _ in Agent(_CFG).stream(
            [Message(role=Role.USER, content="hi")],
            context=AgentContext(session_id="prod:world:2026-06-04"),
        ):
            pass
        assert captured.get("session_id") == "prod:world:2026-06-04"


class TestAgentConfig:
    def test_frozen(self):
        cfg = AgentConfig("p", "m", "t")
        with pytest.raises(AttributeError):
            cfg.prompt_id = "x"  # type: ignore[misc]

    def test_defaults(self):
        assert AgentConfig("p", "m").trace_name is None

    def test_native_web_search_defaults_false(self):
        """New opt-in field defaults off, so every existing config is unaffected."""
        assert AgentConfig("p", "m").native_web_search is False

    def test_native_web_search_opt_in(self):
        """A config may declare it wants native web search."""
        assert AgentConfig("p", "m", native_web_search=True).native_web_search is True

    def test_replace(self):
        from dataclasses import replace

        cfg = AgentConfig("p", "m", "t")
        new = replace(cfg, model_id="new")
        assert new.model_id == "new"
        assert cfg.model_id == "m"


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    async def test_returns_final_message(self, mock_deps):
        result = await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            prompt_vars={"name": "test"},
        )
        assert isinstance(result, Message)
        assert result.text() == "hello"

    async def test_builds_model_client_from_config_model_id(self, mock_deps):
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])
        mock_deps["build_model_client"].assert_awaited_once_with("test-model")

    async def test_tooldefs_passed_to_model(self, mock_deps):
        from app.agent.tooling import tool

        @tool
        async def my_tool(x: str) -> str:
            """A tool.

            Args:
                x: in.
            """
            return ""

        await Agent(_CFG, tools=[my_tool]).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        # the model saw the tool's neutral ToolDef
        tools = mock_deps["model"].complete.await_args.kwargs["tools"]
        assert tools is not None
        assert tools[0].name == "my_tool"

    async def test_no_tools_passes_none(self, mock_deps):
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])
        tools = mock_deps["model"].complete.await_args.kwargs["tools"]
        assert tools is None

    async def test_model_kwargs_forwarded_to_complete(self, mock_deps):
        # model_kwargs (e.g. reasoning_effort=low for the safety guard) must
        # reach the model call — dropping them is a silent behaviour regression.
        await Agent(_CFG, model_kwargs={"reasoning_effort": "low"}).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        kwargs = mock_deps["model"].complete.await_args.kwargs
        assert kwargs["reasoning_effort"] == "low"

    async def test_compiles_prompt_via_langfuse(self, mock_deps):
        await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            prompt_vars={"key": "val"},
        )
        mock_deps["compile_to_messages"].assert_called_once()
        call_kwargs = mock_deps["compile_to_messages"].call_args.kwargs
        assert call_kwargs["key"] == "val"
        assert "currDate" in call_kwargs
        assert "currTime" in call_kwargs

    async def test_curr_time_injected_in_cst(self, mock_deps):
        """全局注入的 currTime / currDate 是 CST（不再 naive 系统时间）。

        旧 bug：``datetime.now().strftime(...)`` 是 naive，容器 TZ 不确定（可能
        UTC），喂给每条 prompt 的"现在"跟 world/life 的 CST 时刻差 8 小时。改成
        显式 CST。钉死 now：真实 UTC 12:30 → CST 20:30、CST 日期 2026-06-03。
        """
        import datetime as _dt

        from app.infra import cst_time

        class _FixedDateTime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                base = cls(2026, 6, 3, 12, 30, 45, tzinfo=_dt.timezone.utc)
                return (
                    base.astimezone(tz) if tz is not None
                    else base.replace(tzinfo=None)
                )

        with patch.object(cst_time, "datetime", _FixedDateTime):
            await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])

        call_kwargs = mock_deps["compile_to_messages"].call_args.kwargs
        assert call_kwargs["currTime"] == "20:30:45", (
            f"currTime 该是 CST（UTC 12:30:45 → CST 20:30:45），实际 {call_kwargs['currTime']!r}"
        )
        assert call_kwargs["currDate"] == "2026-06-03"

    async def test_prompt_messages_prepended(self, mock_deps):
        mock_deps["compile_to_messages"].return_value = [
            Message(role=Role.SYSTEM, content="sys prompt"),
        ]
        user_msg = Message(role=Role.USER, content="hi")
        await Agent(_CFG).run(messages=[user_msg])

        sent = mock_deps["model"].complete.await_args.args[0]
        assert sent[0].role == Role.SYSTEM
        assert sent[0].text() == "sys prompt"
        assert sent[-1].text() == "hi"

    async def test_retries_on_transient_error(self, mock_deps):
        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                Message(role=Role.ASSISTANT, content="retry ok"),
            ]
        )
        result = await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            max_retries=2,
        )
        assert result.text() == "retry ok"
        assert mock_deps["model"].complete.call_count == 2

    async def test_raises_after_max_retries(self, mock_deps):
        mock_deps["model"].complete = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )
        with pytest.raises(APITimeoutError):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")],
                max_retries=2,
            )
        assert mock_deps["model"].complete.call_count == 2

    async def test_passes_context_to_tool_dispatch(self, mock_deps):
        from app.agent.runtime_context import get_context
        from app.agent.tooling import tool
        from app.agent.neutral import ToolCall

        seen: dict = {}

        @tool
        async def ctx_tool(x: str) -> str:
            """Reads context.

            Args:
                x: in.
            """
            seen["persona"] = get_context().persona_id
            return "done"

        # first completion requests the tool, second finishes
        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                Message(
                    role=Role.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="c1", name="ctx_tool", arguments={"x": "v"})],
                ),
                Message(role=Role.ASSISTANT, content="ok"),
            ]
        )
        ctx = AgentContext(message_id="m", chat_id="c", persona_id="luna")
        await Agent(_CFG, tools=[ctx_tool]).run(
            messages=[Message(role=Role.USER, content="go")],
            context=ctx,
        )
        assert seen["persona"] == "luna"


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


class TestStream:
    async def test_yields_chunks(self, mock_deps):
        async def fake_stream(messages, *, tools=None, **kwargs):
            for c in [StreamChunk(text="he"), StreamChunk(text="llo"),
                      StreamChunk(finish_reason="stop")]:
                yield c

        mock_deps["model"].stream = fake_stream

        collected = []
        async for chunk in Agent(_CFG).stream(
            messages=[Message(role=Role.USER, content="hi")],
        ):
            collected.append(chunk)

        texts = [c.text for c in collected if c.text]
        assert "".join(texts) == "hello"

    async def test_no_retry_after_tokens_yielded(self, mock_deps):
        call_count = 0

        async def failing_stream(messages, *, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            yield StreamChunk(text="partial")
            raise APITimeoutError(request=MagicMock())

        mock_deps["model"].stream = failing_stream

        with pytest.raises(APITimeoutError):
            async for _ in Agent(_CFG).stream(
                messages=[Message(role=Role.USER, content="hi")],
                max_retries=3,
            ):
                pass

        assert call_count == 1  # no retry after yield

    async def test_retries_before_first_token(self, mock_deps):
        call_count = 0

        async def failing_then_ok(messages, *, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise APITimeoutError(request=MagicMock())
                yield  # pragma: no cover
            yield StreamChunk(text="ok")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = failing_then_ok

        collected = []
        async for chunk in Agent(_CFG).stream(
            messages=[Message(role=Role.USER, content="hi")],
            max_retries=3,
        ):
            collected.append(chunk)

        assert call_count == 2  # first attempt failed, second succeeded
        assert "".join(c.text or "" for c in collected) == "ok"


# ---------------------------------------------------------------------------
# native web search — runtime decision: when main chat runs on a Gemini-3
# model and the dynamic flag is on, drop the ``search_web`` tool and signal the
# model to use its native google search instead. Any condition false → the tool
# list and the model call are byte-for-byte unchanged (no native signal).
# ---------------------------------------------------------------------------

_NATIVE_CFG = AgentConfig("main", "main-chat-model", "main", native_web_search=True)


def _search_web_tool():
    from app.agent.tooling import tool

    @tool
    async def search_web(query: str) -> str:
        """Search the web.

        Args:
            query: the query.
        """
        return "results"

    return search_web


def _other_tool():
    from app.agent.tooling import tool

    @tool
    async def draw_picture(prompt: str) -> str:
        """Draw a picture.

        Args:
            prompt: the prompt.
        """
        return "drawn"

    return draw_picture


@pytest.fixture()
def flag_on():
    """Patch the dynamic flag ``main_chat_native_web_search`` → True."""
    with patch("app.agent.core.dynamic_config") as dc:
        dc.get_bool.return_value = True
        yield dc


@pytest.fixture()
def flag_off():
    with patch("app.agent.core.dynamic_config") as dc:
        dc.get_bool.return_value = False
        yield dc


class TestNativeWebSearchDecision:
    """The three conditions (cfg opt-in AND model supports AND flag on) plus a
    'search_web is actually present' guard gate the swap. The swap drops
    search_web from the tools the model sees and forwards native_web_search=True;
    otherwise the tools and the model call are unchanged and no signal leaks."""

    async def test_run_all_conditions_true_drops_search_web_and_signals(
        self, mock_deps, flag_on
    ):
        mock_deps["model"].supports_native_web_search = True
        search_web, other = _search_web_tool(), _other_tool()
        await Agent(_NATIVE_CFG, tools=[search_web, other]).run(
            messages=[Message(role=Role.USER, content="今天天气")]
        )
        kw = mock_deps["model"].complete.await_args.kwargs
        tool_names = [t.name for t in (kw["tools"] or [])]
        assert "search_web" not in tool_names
        assert "draw_picture" in tool_names
        assert kw.get("native_web_search") is True
        # the flag was consulted with the documented key + safe default
        flag_on.get_bool.assert_called_once_with(
            "main_chat_native_web_search", default=False
        )

    async def test_run_model_unsupported_keeps_search_web_no_signal(
        self, mock_deps, flag_on
    ):
        mock_deps["model"].supports_native_web_search = False
        search_web, other = _search_web_tool(), _other_tool()
        await Agent(_NATIVE_CFG, tools=[search_web, other]).run(
            messages=[Message(role=Role.USER, content="今天天气")]
        )
        kw = mock_deps["model"].complete.await_args.kwargs
        tool_names = [t.name for t in (kw["tools"] or [])]
        assert "search_web" in tool_names
        assert "native_web_search" not in kw
        # short-circuit: an unsupported model never reaches the (blocking) flag read
        flag_on.get_bool.assert_not_called()

    async def test_run_flag_off_keeps_search_web_no_signal(
        self, mock_deps, flag_off
    ):
        mock_deps["model"].supports_native_web_search = True
        search_web, other = _search_web_tool(), _other_tool()
        await Agent(_NATIVE_CFG, tools=[search_web, other]).run(
            messages=[Message(role=Role.USER, content="今天天气")]
        )
        kw = mock_deps["model"].complete.await_args.kwargs
        tool_names = [t.name for t in (kw["tools"] or [])]
        assert "search_web" in tool_names
        assert "native_web_search" not in kw

    async def test_run_cfg_not_opted_in_keeps_search_web_no_signal(
        self, mock_deps, flag_on
    ):
        # world/life/guard configs never declare native_web_search → even a
        # Gemini-3 model with the flag on must not trigger the swap.
        mock_deps["model"].supports_native_web_search = True
        search_web, other = _search_web_tool(), _other_tool()
        await Agent(_CFG, tools=[search_web, other]).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        kw = mock_deps["model"].complete.await_args.kwargs
        tool_names = [t.name for t in (kw["tools"] or [])]
        assert "search_web" in tool_names
        assert "native_web_search" not in kw
        # short-circuit: a non-opted agent never reaches the (blocking) flag read
        flag_on.get_bool.assert_not_called()

    async def test_run_no_search_web_present_does_not_signal(
        self, mock_deps, flag_on
    ):
        # opted in, model supports, flag on — but the agent never mounted
        # search_web → must NOT enable native search for free (cost guard).
        mock_deps["model"].supports_native_web_search = True
        other = _other_tool()
        await Agent(_NATIVE_CFG, tools=[other]).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        kw = mock_deps["model"].complete.await_args.kwargs
        tool_names = [t.name for t in (kw["tools"] or [])]
        assert tool_names == ["draw_picture"]
        assert "native_web_search" not in kw

    async def test_non_native_agent_with_flag_present_is_byte_for_byte(
        self, mock_deps, flag_on
    ):
        # A world/life-style agent (not opted in) on this path must call the
        # model exactly as before: tools unchanged, no native signal, even when
        # the global flag happens to be on.
        mock_deps["model"].supports_native_web_search = True
        search_web = _search_web_tool()
        await Agent(_CFG, tools=[search_web]).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        kw = mock_deps["model"].complete.await_args.kwargs
        assert [t.name for t in (kw["tools"] or [])] == ["search_web"]
        assert "native_web_search" not in kw

    async def test_stream_all_conditions_true_drops_search_web_and_signals(
        self, mock_deps, flag_on
    ):
        captured: dict = {}

        async def fake_stream(messages, *, tools=None, **kwargs):
            captured["tools"] = tools
            captured["kwargs"] = kwargs
            yield StreamChunk(text="hi")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = fake_stream
        mock_deps["model"].supports_native_web_search = True
        search_web, other = _search_web_tool(), _other_tool()
        async for _ in Agent(_NATIVE_CFG, tools=[search_web, other]).stream(
            messages=[Message(role=Role.USER, content="今天天气")]
        ):
            pass
        tool_names = [t.name for t in (captured["tools"] or [])]
        assert "search_web" not in tool_names
        assert "draw_picture" in tool_names
        assert captured["kwargs"].get("native_web_search") is True

    async def test_stream_flag_off_keeps_search_web_no_signal(
        self, mock_deps, flag_off
    ):
        captured: dict = {}

        async def fake_stream(messages, *, tools=None, **kwargs):
            captured["tools"] = tools
            captured["kwargs"] = kwargs
            yield StreamChunk(text="hi")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = fake_stream
        mock_deps["model"].supports_native_web_search = True
        search_web = _search_web_tool()
        async for _ in Agent(_NATIVE_CFG, tools=[search_web]).stream(
            messages=[Message(role=Role.USER, content="今天天气")]
        ):
            pass
        assert [t.name for t in (captured["tools"] or [])] == ["search_web"]
        assert "native_web_search" not in captured["kwargs"]


# ---------------------------------------------------------------------------
# empty prompt_id guard
# ---------------------------------------------------------------------------


class TestEmptyPromptGuard:
    async def test_run_rejects_empty_prompt_id(self, mock_deps):
        with pytest.raises(ValueError, match="non-empty prompt_id"):
            await Agent(_NO_PROMPT_CFG).run(
                messages=[Message(role=Role.USER, content="hi")]
            )

    async def test_stream_rejects_empty_prompt_id(self, mock_deps):
        with pytest.raises(ValueError, match="non-empty prompt_id"):
            async for _ in Agent(_NO_PROMPT_CFG).stream(
                messages=[Message(role=Role.USER, content="hi")]
            ):
                pass

    async def test_extract_allows_empty_prompt_id(self, mock_deps):
        """Guard agents have empty prompt_id and only use extract()."""

        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        result = await Agent(_NO_PROMPT_CFG).extract(
            Out, messages=[Message(role=Role.USER, content="test")]
        )
        assert result.v == "ok"
        mock_deps["get_prompt"].assert_not_called()


# ---------------------------------------------------------------------------
# recursion_limit
# ---------------------------------------------------------------------------


class TestRecursionLimit:
    async def test_default_recursion_limit_caps_tool_loop(self, mock_deps):
        from app.agent.neutral import ToolCall
        from app.agent.tooling import tool

        @tool
        async def loop_tool(x: str) -> str:
            """loops.

            Args:
                x: in.
            """
            return "again"

        # always request a tool → loop bounded by the model-call budget
        # (default 6, matching the legacy LangGraph ~6 model calls).
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c", name="loop_tool", arguments={"x": "v"})],
            )
        )
        await Agent(_CFG, tools=[loop_tool]).run(
            messages=[Message(role=Role.USER, content="go")]
        )
        assert mock_deps["model"].complete.call_count == 6

    async def test_custom_recursion_limit(self, mock_deps):
        from app.agent.neutral import ToolCall
        from app.agent.tooling import tool

        @tool
        async def loop_tool(x: str) -> str:
            """loops.

            Args:
                x: in.
            """
            return "again"

        cfg = AgentConfig("p", "m", "t", recursion_limit=4)
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c", name="loop_tool", arguments={"x": "v"})],
            )
        )
        await Agent(cfg, tools=[loop_tool]).run(
            messages=[Message(role=Role.USER, content="go")]
        )
        assert mock_deps["model"].complete.call_count == 4


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestExtract:
    async def test_returns_pydantic_model(self, mock_deps):
        class Score(BaseModel):
            name: str
            value: float

        mock_deps["model"].structured = AsyncMock(
            return_value={"name": "test", "value": 0.9}
        )

        result = await Agent(_EXTRACT_CFG).extract(
            Score,
            messages=[Message(role=Role.USER, content="rate this")],
            prompt_vars={"key": "val"},
        )

        assert isinstance(result, Score)
        assert result.name == "test"
        assert result.value == 0.9

    async def test_passes_response_model_schema(self, mock_deps):
        class Score(BaseModel):
            name: str

        mock_deps["model"].structured = AsyncMock(return_value={"name": "x"})

        await Agent(_EXTRACT_CFG).extract(
            Score, messages=[Message(role=Role.USER, content="t")]
        )
        schema = mock_deps["model"].structured.await_args.kwargs["schema"]
        assert "properties" in schema
        assert "name" in schema["properties"]

    async def test_extract_forwards_model_kwargs(self, mock_deps):
        # the safety guard configures Agent(..., model_kwargs={"reasoning_effort":
        # "low"}) and only calls extract — the kwargs must reach structured().
        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(
            _EXTRACT_CFG, model_kwargs={"reasoning_effort": "low"}
        ).extract(Out, messages=[Message(role=Role.USER, content="t")])
        kwargs = mock_deps["model"].structured.await_args.kwargs
        assert kwargs["reasoning_effort"] == "low"

    async def test_extract_retries(self, mock_deps):
        class Result(BaseModel):
            ok: bool

        mock_deps["model"].structured = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                {"ok": True},
            ]
        )

        result = await Agent(_EXTRACT_CFG).extract(
            Result,
            messages=[Message(role=Role.USER, content="test")],
            max_retries=2,
        )
        assert result.ok is True
        assert mock_deps["model"].structured.call_count == 2

    async def test_extract_skips_prompt_when_empty(self, mock_deps):
        """Guard agents have empty prompt_id — extract should not call get_prompt."""

        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(_NO_PROMPT_CFG).extract(
            Out,
            messages=[Message(role=Role.USER, content="test")],
        )
        mock_deps["get_prompt"].assert_not_called()

    async def test_extract_with_chat_prompt_messages_prepended(self, mock_deps):
        """Chat prompt should produce multiple messages prepended to input."""

        class Out(BaseModel):
            v: str

        mock_deps["compile_to_messages"].return_value = [
            Message(role=Role.SYSTEM, content="You are a guard."),
            Message(role=Role.USER, content="Check: test input"),
        ]
        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(_EXTRACT_CFG).extract(
            Out, messages=[Message(role=Role.USER, content="more")]
        )

        sent = mock_deps["model"].structured.await_args.args[0]
        assert len(sent) == 3
        assert sent[0].role == Role.SYSTEM
        assert sent[1].role == Role.USER
        assert sent[2].text() == "more"

    async def test_extract_threads_session_id_to_root_span(self):
        """``extract(session_id=...)`` tags the langfuse trace's session.

        async-internal structured judgments (e.g. world 在场匹配) want their trace
        grouped into an actor's day session, exactly like ``run`` / ``stream``.
        ``_root_span`` already supports ``session_id``; ``extract`` must thread it
        through (None by default keeps the status quo — no session tag).
        """
        from contextlib import contextmanager

        class Out(BaseModel):
            v: str

        model = AsyncMock()
        model.structured = AsyncMock(return_value={"v": "ok"})

        captured: dict = {}

        @contextmanager
        def _capturing_span(**kwargs):
            captured.update(kwargs)
            yield MagicMock()

        with (
            patch(
                "app.agent.core.build_model_client",
                new_callable=AsyncMock,
                return_value=model,
            ),
            patch("app.agent.core._root_span", _capturing_span),
        ):
            await Agent(_NO_PROMPT_CFG).extract(
                Out,
                messages=[Message(role=Role.USER, content="t")],
                session_id="coe-t2:world:2026-06-16",
            )

        assert captured.get("session_id") == "coe-t2:world:2026-06-16"


# ---------------------------------------------------------------------------
# session续接 — stateful continuation across runs via a session_id
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_store_db(test_db):
    """Back the session store with the real PG ``SessionTranscript`` table.

    The session transcript store is now durable PG: ``app.agent.session`` reads /
    writes via ``select_latest`` / ``insert_append`` against the app DB, which the
    ``test_db`` fixture repoints at the test container. Building the table here
    lets the Agent's session reads/writes round-trip through real PG so tests can
    assert what was persisted. Returns the test engine.
    """
    from app.domain.session_transcript import SessionTranscript
    from tests.runtime.conftest import migrate

    await migrate(SessionTranscript, test_db)
    return test_db


@pytest.mark.integration
class TestSessionContinuation:
    """``Agent.run / stream`` with a ``session_id`` reads the stored transcript,
    prepends it (between the system prompt and the new messages) so the model
    continues, and appends this round's new messages back to durable PG. Without a
    session_id the path is byte-for-byte unchanged and never touches the store
    (chat status quo)."""

    async def test_second_run_input_carries_first_round_transcript(
        self, mock_deps, session_store_db
    ):
        from app.agent.neutral import ToolCall

        # First round: a tool call + result, then a final reply. The assistant
        # tool call carries a provider-private signature that MUST survive replay.
        from app.agent.tooling import tool

        @tool
        async def world_tool(x: str) -> str:
            """A world tool.

            Args:
                x: in.
            """
            return "餐桌已收"

        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                Message(
                    role=Role.ASSISTANT,
                    content="",
                    reasoning_content="想了想",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="world_tool",
                            arguments={"x": "v"},
                            signature=b"\x00\xffsig",
                        )
                    ],
                ),
                Message(role=Role.ASSISTANT, content="第一轮做完了"),
            ]
        )
        sid = "prod:world:2026-06-04"
        await Agent(_CFG, tools=[world_tool]).run(
            messages=[Message(role=Role.USER, content="第一轮醒")],
            session_id=sid,
        )

        # Second round: a single reply. Capture what the model is fed.
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(role=Role.ASSISTANT, content="第二轮")
        )
        await Agent(_CFG, tools=[world_tool]).run(
            messages=[Message(role=Role.USER, content="第二轮醒")],
            session_id=sid,
        )

        sent = mock_deps["model"].complete.await_args.args[0]
        texts = [m.text() for m in sent]
        # the system prompt is first, then the FIRST round's transcript, then
        # this round's new message — the model is genuinely continuing.
        assert "第一轮醒" in texts
        assert "第一轮做完了" in texts
        assert "第二轮醒" in texts
        # lossless replay: the stored assistant tool-call kept its signature.
        replayed_assistant = next(
            m for m in sent if m.role == Role.ASSISTANT and m.tool_calls
        )
        assert replayed_assistant.tool_calls[0].signature == b"\x00\xffsig"
        assert replayed_assistant.reasoning_content == "想了想"
        # the tool RESULT from round 1 is replayed too
        tool_msg = next(m for m in sent if m.role == Role.TOOL)
        assert tool_msg.text() == "餐桌已收"
        # ordering: system prompt precedes replayed history precedes new turn
        assert sent[0].role == Role.SYSTEM
        assert texts.index("第一轮醒") < texts.index("第二轮醒")

    async def test_no_session_id_does_not_touch_store(self, mock_deps):
        # Without a session_id the run must not read or write the store at all.
        from app.agent import session as session_mod

        loads: list = []
        appends: list = []
        orig_load = session_mod.load_session
        orig_append = session_mod.append_session

        async def _spy_load(*a, **k):
            loads.append(a)
            return await orig_load(*a, **k)

        async def _spy_append(*a, **k):
            appends.append(a)
            return await orig_append(*a, **k)

        import app.agent.core as core_mod

        with (
            patch.object(core_mod, "load_session", _spy_load),
            patch.object(core_mod, "append_session", _spy_append),
        ):
            result = await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")]
            )
        assert result.text() == "hello"
        assert loads == []
        assert appends == []

    async def test_no_session_id_input_is_unchanged(self, mock_deps):
        # byte-for-byte status quo: prompt + messages, nothing prepended.
        mock_deps["compile_to_messages"].return_value = [
            Message(role=Role.SYSTEM, content="sys"),
        ]
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])
        sent = mock_deps["model"].complete.await_args.args[0]
        assert [m.text() for m in sent] == ["sys", "hi"]

    async def test_run_appends_round_to_durable_store(
        self, mock_deps, session_store_db
    ):
        from app.agent.session import load_session

        sid = "prod:world:2026-06-04"
        await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="醒了")],
            session_id=sid,
        )
        stored = await load_session(sid)
        # the round (user input + assistant reply) was persisted to PG
        assert [m.text() for m in stored] == ["醒了", "hello"]

    async def test_session_id_drives_langfuse_session_grouping(self, mock_deps):
        # The same id is both the session store key AND the langfuse session tag
        # (decision 3). A run with a session_id groups its trace into that
        # session even without a context.
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None):
            captured["session_id"] = session_id
            yield MagicMock()

        with (
            patch("app.agent.core._root_span", _spy_span),
            patch("app.agent.core.load_session", new_callable=AsyncMock,
                  return_value=[]),
            patch("app.agent.core.append_session", new_callable=AsyncMock),
        ):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")],
                session_id="prod:world:2026-06-04",
            )
        assert captured["session_id"] == "prod:world:2026-06-04"

    async def test_stream_continuation_carries_prior_transcript(
        self, mock_deps, session_store_db
    ):
        # First round via stream: text reply only.
        async def round1(messages, *, tools=None, **kwargs):
            yield StreamChunk(text="第一轮回复")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = round1
        sid = "prod:akao:2026-06-04"
        async for _ in Agent(_CFG).stream(
            messages=[Message(role=Role.USER, content="第一轮")],
            session_id=sid,
        ):
            pass

        # Second round: capture what the model is fed.
        async def round2(messages, *, tools=None, **kwargs):
            yield StreamChunk(text="第二轮回复")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = round2
        async for _ in Agent(_CFG).stream(
            messages=[Message(role=Role.USER, content="第二轮")],
            session_id=sid,
        ):
            pass

        # assert via the persisted transcript (stream is a plain fn, no mock to
        # introspect args): round 1's user + assistant reply landed, and round 2
        # appended on top — proving the stream path also reads + writes the
        # session.
        from app.agent.session import load_session

        stored = await load_session(sid)
        texts = [m.text() for m in stored]
        assert texts == ["第一轮", "第一轮回复", "第二轮", "第二轮回复"]


@pytest.mark.integration
class TestSessionWriteFailureSwallowed:
    """The session store is a *working cache*: a write-back failure must NOT turn
    an already-completed round (with its tool side effects emit/move/state writes
    already done) into a failed round. If durable nodes saw the exception they'd
    re-deliver / DLQ a round whose effects already happened. So append failures
    are logged and swallowed; the run still returns its final assistant message,
    and next round cold-starts from PG hard facts (symmetric to load's missing
    key → cold start)."""

    async def test_run_returns_result_when_append_session_raises(
        self, mock_deps, session_store_db, caplog
    ):
        import logging

        from app.capabilities._errors import CapabilityCallFailed

        with patch(
            "app.agent.core.append_session",
            new_callable=AsyncMock,
            side_effect=CapabilityCallFailed("session transcript write failed"),
        ):
            with caplog.at_level(logging.WARNING):
                result = await Agent(_CFG).run(
                    messages=[Message(role=Role.USER, content="醒了")],
                    session_id="prod:world:2026-06-04",
                )
        # the round's final assistant message is still returned, not an error
        assert result.text() == "hello"
        # the failure was logged (observable), not silently dropped
        assert any(
            "session" in r.message.lower() for r in caplog.records
        )

    async def test_stream_completes_when_append_session_raises(
        self, mock_deps, session_store_db, caplog
    ):
        import logging

        from app.capabilities._errors import CapabilityCallFailed

        async def fake_stream(messages, *, tools=None, **kwargs):
            yield StreamChunk(text="第一轮回复")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = fake_stream

        with patch(
            "app.agent.core.append_session",
            new_callable=AsyncMock,
            side_effect=CapabilityCallFailed("session transcript write failed"),
        ):
            collected = []
            with caplog.at_level(logging.WARNING):
                async for chunk in Agent(_CFG).stream(
                    messages=[Message(role=Role.USER, content="第一轮")],
                    session_id="prod:akao:2026-06-04",
                ):
                    collected.append(chunk)
        # the stream produced its chunks and finished cleanly despite the
        # write-back blowing up — no exception propagated to the consumer.
        texts = "".join(c.text or "" for c in collected)
        assert texts == "第一轮回复"
        assert any("session" in r.message.lower() for r in caplog.records)
