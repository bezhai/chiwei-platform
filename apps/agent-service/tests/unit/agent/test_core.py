"""test_core.py -- Agent unified interface tests (post-langchain cutover).

Covers the Agent layer over the neutral ``ModelClient`` (no langchain /
create_agent / CallbackHandler):
  - Agent.run(): returns final neutral Message, retry on transient errors,
    prompt compilation + message threading, recursion_limit passthrough.
  - Agent.extract(): structured output validated into a Pydantic model.
  - What an AgentSpec decides: model (fixed or from Dynamic Config), budget of
    model calls, retries, model kwargs; the session comes from the Call.

The ReAct loop control flow itself is de-risked separately in
``test_react_loop.py`` against a scripted fake ModelClient; here we mock the
loop functions / model client to assert the Agent's own responsibilities.
"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APITimeoutError
from pydantic import BaseModel

from app.agent.contracts import AgentSpec, FromConfig, ToolSpec
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.round import Call, open_call

pytestmark = pytest.mark.unit

# Reusable test specs
_CFG = AgentSpec(
    segments=("test_prompt",), model="test-model", trace_name="test-agent", max_retries=2
)
_EXTRACT_CFG = AgentSpec(
    segments=("relationship_filter",),
    model="relationship-model",
    trace_name="relationship-filter",
    max_retries=2,
)


def _specs(*tools) -> list[ToolSpec]:
    """The tools, declared the way a plugin declares them (none ends the round)."""
    return [ToolSpec(t, trim="kept") for t in tools]


def _call(spec: AgentSpec = _CFG) -> Call:
    """A call of ``spec`` without a session, named after its one prompt."""
    return open_call(spec.segments[0], spec, lane="prod")


def _call_in(session: str, spec: AgentSpec = _CFG) -> Call:
    """A call of ``spec`` whose session is ``session``."""
    return Call(agent=spec.segments[0], session=session, round=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Prompts:
    """Stands in for a resolved ``PromptSet``: renders ``system`` for any agent and records each
    render (the agent, the values given at the call)."""

    def __init__(self) -> None:
        self.system = [Message(role=Role.SYSTEM, content="You are a helpful assistant.")]
        self.rendered: list[tuple[str, dict | None]] = []

    def messages(self, agent, values=None):
        self.rendered.append((agent, None if values is None else dict(values)))
        return list(self.system)

    def versions(self, agent):
        return [(agent, "production", 1)]

    def single_prompt(self, agent):
        return None


# What every call in this module renders from; mock_deps empties it per test.
_PROMPTS = _Prompts()


@pytest.fixture()
def mock_deps():
    """Patch build_model_client and the root span; the prompt set is :data:`_PROMPTS`.

    The root span is patched to a no-op so tracing/langfuse never reaches the
    network in unit tests. The prompt set renders a neutral SYSTEM message by
    default.
    """
    _PROMPTS.__init__()
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
        patch("app.agent.core._root_span", _noop_span),
    ):
        yield {
            "build_model_client": mock_build,
            "prompts": _PROMPTS,
            "model": model,
        }


# ---------------------------------------------------------------------------
# root span
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


class TestRootSpanTraceName:
    """A root span starts a trace of its own unless it is opened inside another
    span; ``update_trace`` decides whether it names the trace and sets its input
    (the output check runs inside her round's tool and leaves her trace alone)."""

    def test_update_trace_names_the_trace_after_the_span(self):
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with core._root_span(name="world-engine", input=[1], update_trace=True):
                pass
        kw = client.update_current_trace.call_args.kwargs
        assert kw["name"] == "world-engine"
        assert kw["input"] == [1]

    def test_no_update_when_update_trace_false(self):
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


class TestRootSpanAppAttributes:
    """The ``app:`` tag and ``metadata.app`` name the App this process runs as
    (``APP_NAME``, injected by PaaS per Deployment). One image runs both the
    agent-service and the world App; a world round's trace must not be filed
    under ``app:agent-service``."""

    @staticmethod
    def _trace_kwargs() -> dict:
        from app.agent import core

        client = MagicMock()
        with (
            patch.object(core, "_get_trace_client", return_value=client),
            patch.object(core, "get_lane", return_value=None),
            patch.object(core, "current_deployment_lane", return_value=None),
        ):
            with core._root_span(name="world-round", input=[], update_trace=True):
                pass
        return client.update_current_trace.call_args.kwargs

    def test_world_process_tags_its_traces_as_world(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "world")

        kw = self._trace_kwargs()

        assert "app:world" in kw["tags"]
        assert "app:agent-service" not in kw["tags"]
        assert kw["metadata"]["app"] == "world"

    def test_agent_service_process_keeps_app_agent_service(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "agent-service")

        kw = self._trace_kwargs()

        assert kw["tags"][0] == "app:agent-service"
        assert kw["metadata"]["app"] == "agent-service"

    def test_unset_app_name_falls_back_to_the_default_app(self, monkeypatch):
        monkeypatch.delenv("APP_NAME", raising=False)

        kw = self._trace_kwargs()

        assert kw["tags"][0] == "app:agent-service"
        assert kw["metadata"]["app"] == "agent-service"


class TestRunSessionPlumbing:
    """The session rides on the Call its caller opened: the root span tags the
    trace with it, and an agent without a session leaves the trace's session
    untouched."""

    async def test_run_threads_the_calls_session_to_root_span(self, mock_deps):
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None, prompt=None):
            captured["session_id"] = session_id
            yield MagicMock()

        with patch("app.agent.core._root_span", _spy_span):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")],
                call=_call_in("prod:luna:2026-06-04"), prompts=_PROMPTS,
            )
        assert captured["session_id"] == "prod:luna:2026-06-04"

    async def test_run_without_a_session_passes_none(self, mock_deps):
        captured: dict = {}
        from contextlib import contextmanager

        @contextmanager
        def _spy_span(*, name, input, update_trace, session_id=None, prompt=None):
            captured["session_id"] = session_id
            yield MagicMock()

        with patch("app.agent.core._root_span", _spy_span):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS
            )
        assert captured["session_id"] is None

class TestModelCacheSessionId:
    """The call's session is forwarded to model.complete as the
    prompt-cache key, so the azure adapter can wire it into the gateway's
    session cache header. None when there is no session (adapter no-ops)."""

    async def test_run_forwards_the_calls_session_to_model(self, mock_deps):
        await Agent(_CFG).run(
            [Message(role=Role.USER, content="hi")],
            call=_call_in("prod:world:2026-06-04"), prompts=_PROMPTS,
        )
        kw = mock_deps["model"].complete.call_args.kwargs
        assert kw.get("session_id") == "prod:world:2026-06-04"

    async def test_run_without_session_forwards_none(self, mock_deps):
        await Agent(_CFG).run([Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS)
        kw = mock_deps["model"].complete.call_args.kwargs
        assert kw.get("session_id") is None

# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    async def test_returns_final_message(self, mock_deps):
        result = await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            values={"name": "test"}, call=_call(), prompts=_PROMPTS,
        )
        assert isinstance(result, Message)
        assert result.text() == "hello"

    async def test_builds_model_client_from_config_model_id(self, mock_deps):
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS)
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

        await Agent(_CFG, tools=_specs(my_tool)).run(
            messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS
        )
        # the model saw the tool's neutral ToolDef
        tools = mock_deps["model"].complete.await_args.kwargs["tools"]
        assert tools is not None
        assert tools[0].name == "my_tool"

    async def test_no_tools_passes_none(self, mock_deps):
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS)
        tools = mock_deps["model"].complete.await_args.kwargs["tools"]
        assert tools is None

    async def test_model_kwargs_forwarded_to_complete(self, mock_deps):
        # model_kwargs (e.g. reasoning_effort=low for the safety guard) must
        # reach the model call — dropping them is a silent behaviour regression.
        spec = replace(_CFG, model_kwargs={"reasoning_effort": "low"})
        await Agent(spec).run(
            messages=[Message(role=Role.USER, content="hi")], call=_call(spec), prompts=_PROMPTS
        )
        kwargs = mock_deps["model"].complete.await_args.kwargs
        assert kwargs["reasoning_effort"] == "low"

    async def test_renders_the_calls_agent_from_its_prompt_set(self, mock_deps):
        await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            values={"key": "val"}, call=_call(), prompts=_PROMPTS,
        )
        assert mock_deps["prompts"].rendered == [("test_prompt", {"key": "val"})]

    async def test_model_and_budget_from_dynamic_config_are_read_at_the_call(
        self, mock_deps, monkeypatch
    ):
        from inner_shared.dynamic_config import dynamic_config

        from app.agent.neutral import ToolCall
        from app.agent.tooling import tool

        @tool
        async def loop_tool() -> str:
            """loops."""
            return "again"

        config = {"npc_model": "", "main_model": "main-model", "budget": "2"}
        monkeypatch.setattr(
            dynamic_config, "get", lambda k, default="": config.get(k, default)
        )
        spec = AgentSpec(
            segments=("p",),
            model=FromConfig(keys=("npc_model", "main_model"), default="fallback"),
            trace_name="t",
            recursion_limit=FromConfig(keys=("budget",), default=12),
        )
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c", name="loop_tool", arguments={})],
            )
        )
        await Agent(spec, tools=_specs(loop_tool)).run(
            messages=[Message(role=Role.USER, content="go")], call=_call(spec), prompts=_PROMPTS
        )
        mock_deps["build_model_client"].assert_awaited_once_with("main-model")
        assert mock_deps["model"].complete.call_count == 3

    async def test_no_time_is_put_into_the_prompt(self, mock_deps):
        """The prompt gets the values its caller gives and nothing else: no ``currDate`` /
        ``currTime`` (no template declares them, and the strict check would refuse them)."""
        await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS
        )
        assert mock_deps["prompts"].rendered == [("test_prompt", None)]

    async def test_prompt_messages_prepended(self, mock_deps):
        mock_deps["prompts"].system = [
            Message(role=Role.SYSTEM, content="sys prompt"),
        ]
        user_msg = Message(role=Role.USER, content="hi")
        await Agent(_CFG).run(messages=[user_msg], call=_call(), prompts=_PROMPTS)

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
            messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS,
        )
        assert result.text() == "retry ok"
        assert mock_deps["model"].complete.call_count == 2

    async def test_raises_after_max_retries(self, mock_deps):
        mock_deps["model"].complete = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )
        with pytest.raises(APITimeoutError):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS,
            )
        assert mock_deps["model"].complete.call_count == 2

    async def test_tools_run_in_the_call_being_run(self, mock_deps):
        from app.agent.neutral import ToolCall
        from app.agent.round import current_call
        from app.agent.tooling import tool

        seen: dict = {}

        @tool
        async def ctx_tool(x: str) -> str:
            """Reads its call.

            Args:
                x: in.
            """
            seen["call"] = current_call()
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
        call = _call()
        await Agent(_CFG, tools=_specs(ctx_tool)).run(
            messages=[Message(role=Role.USER, content="go")], call=call, prompts=_PROMPTS
        )
        assert seen["call"] is call

    @pytest.mark.parametrize("ends_round", [True, False])
    async def test_whether_a_tool_ends_the_run_is_what_its_declaration_says(
        self, mock_deps, ends_round
    ):
        from app.agent.core import _NEVER_RAN
        from app.agent.neutral import ToolCall
        from app.agent.tooling import tool

        @tool
        async def finish() -> str:
            """Finish."""
            return "done"

        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                Message(
                    role=Role.ASSISTANT,
                    content="",
                    tool_calls=[
                        ToolCall(id="c1", name="finish", arguments={}),
                        ToolCall(id="c2", name="finish", arguments={}),
                    ],
                ),
                Message(role=Role.ASSISTANT, content="还在。"),
            ]
        )
        sink: list[Message] = []
        await Agent(
            _CFG, tools=[ToolSpec(finish, trim="kept", ends_round=ends_round)]
        ).run(
            messages=[Message(role=Role.USER, content="go")],
            call=_call(),
            prompts=_PROMPTS,
            transcript_sink=sink,
        )

        answered = [m.text() for m in sink if m.role is Role.TOOL]
        if ends_round:
            assert mock_deps["model"].complete.await_count == 1
            assert answered == ["done", _NEVER_RAN]
        else:
            assert mock_deps["model"].complete.await_count == 2
            assert answered == ["done", "done"]


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
        await Agent(_CFG, tools=_specs(loop_tool)).run(
            messages=[Message(role=Role.USER, content="go")], call=_call(), prompts=_PROMPTS
        )
        # 6 turns with tools, then one toolless call so the run ends on what the
        # assistant says instead of on a tool-call turn with no text.
        assert mock_deps["model"].complete.call_count == 7
        assert mock_deps["model"].complete.call_args.kwargs["tools"] is None

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

        cfg = AgentSpec(segments=("p",), model="m", trace_name="t", recursion_limit=4)
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c", name="loop_tool", arguments={"x": "v"})],
            )
        )
        await Agent(cfg, tools=_specs(loop_tool)).run(
            messages=[Message(role=Role.USER, content="go")], call=_call(cfg), prompts=_PROMPTS
        )
        assert mock_deps["model"].complete.call_count == 5
        assert mock_deps["model"].complete.call_args.kwargs["tools"] is None


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
            values={"key": "val"}, call=_call(_EXTRACT_CFG), prompts=_PROMPTS,
        )

        assert isinstance(result, Score)
        assert result.name == "test"
        assert result.value == 0.9

    async def test_passes_response_model_schema(self, mock_deps):
        class Score(BaseModel):
            name: str

        mock_deps["model"].structured = AsyncMock(return_value={"name": "x"})

        await Agent(_EXTRACT_CFG).extract(
            Score, messages=[Message(role=Role.USER, content="t")], call=_call(_EXTRACT_CFG), prompts=_PROMPTS
        )
        schema = mock_deps["model"].structured.await_args.kwargs["schema"]
        assert "properties" in schema
        assert "name" in schema["properties"]

    async def test_extract_forwards_model_kwargs(self, mock_deps):
        # the safety guard's spec carries model_kwargs={"reasoning_effort": "low"}
        # and only calls extract — the kwargs must reach structured().
        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        spec = replace(_EXTRACT_CFG, model_kwargs={"reasoning_effort": "low"})
        await Agent(spec).extract(
            Out, messages=[Message(role=Role.USER, content="t")], call=_call(spec), prompts=_PROMPTS
        )
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
            messages=[Message(role=Role.USER, content="test")], call=_call(_EXTRACT_CFG), prompts=_PROMPTS,
        )
        assert result.ok is True
        assert mock_deps["model"].structured.call_count == 2

    async def test_extract_with_chat_prompt_messages_prepended(self, mock_deps):
        """Chat prompt should produce multiple messages prepended to input."""

        class Out(BaseModel):
            v: str

        mock_deps["prompts"].system = [
            Message(role=Role.SYSTEM, content="You are a guard."),
            Message(role=Role.USER, content="Check: test input"),
        ]
        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(_EXTRACT_CFG).extract(
            Out, messages=[Message(role=Role.USER, content="more")], call=_call(_EXTRACT_CFG), prompts=_PROMPTS
        )

        sent = mock_deps["model"].structured.await_args.args[0]
        assert len(sent) == 3
        assert sent[0].role == Role.SYSTEM
        assert sent[1].role == Role.USER
        assert sent[2].text() == "more"

    async def test_extract_threads_the_calls_session_to_root_span(self, mock_deps):
        """An extract whose call has a session tags the langfuse trace's session,
        exactly like ``run``; a call without one leaves it untouched."""
        from contextlib import contextmanager

        class Out(BaseModel):
            v: str

        captured: dict = {}

        @contextmanager
        def _capturing_span(**kwargs):
            captured.update(kwargs)
            yield MagicMock()

        with patch("app.agent.core._root_span", _capturing_span):
            await Agent(_EXTRACT_CFG).extract(
                Out,
                messages=[Message(role=Role.USER, content="t")],
                call=_call_in("coe-t2:world:2026-06-16", _EXTRACT_CFG), prompts=_PROMPTS,
            )

        assert captured.get("session_id") == "coe-t2:world:2026-06-16"


# ---------------------------------------------------------------------------
# transcript_sink — the round's own messages, handed back for the caller to store
# ---------------------------------------------------------------------------


class TestTranscriptSink:
    """``Agent.run`` reads and writes no store. A caller that keeps a continuous
    context passes the prior messages in ``messages`` and gets this round's new
    messages back through ``transcript_sink``, so it decides when and with what
    else the round is persisted (``app.agent.continuity``)."""

    async def test_sink_collects_tool_call_result_and_final_reply(self, mock_deps):
        from app.agent.neutral import ToolCall, TurnPart
        from app.agent.tooling import tool

        @tool
        async def world_tool(x: str) -> str:
            """A world tool.

            Args:
                x: in.
            """
            return "餐桌已收"

        call = ToolCall(
            id="c1",
            name="world_tool",
            arguments={"x": "v"},
            signature=b"\x00\xffsig",
        )
        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                Message.from_model_turn(
                    [
                        TurnPart.from_thought("想了想", signature=b"\x01sig-t"),
                        TurnPart.from_tool_call(call),
                    ],
                    [call],
                ),
                Message(role=Role.ASSISTANT, content="这一轮做完了"),
            ]
        )
        produced: list[Message] = []
        result = await Agent(_CFG, tools=_specs(world_tool)).run(
            messages=[Message(role=Role.USER, content="醒了")],
            transcript_sink=produced, call=_call(), prompts=_PROMPTS,
        )

        assert result.text() == "这一轮做完了"
        # the sink holds ONLY what the loop produced — the caller already has the
        # input it handed in, and duplicating it here would store it twice.
        assert [m.role for m in produced] == [Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
        # lossless: the provider-private blob is still on the in-memory message.
        assert produced[0].tool_calls[0].signature == b"\x00\xffsig"
        assert produced[0].thought_text() == "想了想"
        assert produced[0].turn_parts[0].signature == b"\x01sig-t"
        assert produced[1].text() == "餐桌已收"
        assert produced[1].tool_call_id == "c1"

    async def test_prior_messages_are_fed_to_the_model_in_order(self, mock_deps):
        mock_deps["prompts"].system = [
            Message(role=Role.SYSTEM, content="sys"),
        ]
        await Agent(_CFG).run(
            messages=[
                Message(role=Role.USER, content="上一轮醒"),
                Message(role=Role.ASSISTANT, content="上一轮说的"),
                Message(role=Role.USER, content="这一轮醒"),
            ], call=_call(), prompts=_PROMPTS
        )
        sent = mock_deps["model"].complete.await_args.args[0]
        assert [m.text() for m in sent] == [
            "sys",
            "上一轮醒",
            "上一轮说的",
            "这一轮醒",
        ]

    async def test_without_a_sink_the_run_is_unchanged(self, mock_deps):
        result = await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")], call=_call(), prompts=_PROMPTS
        )
        assert result.text() == "hello"

    async def test_a_retried_attempt_does_not_leave_a_half_written_round(
        self, mock_deps
    ):
        """A transient failure retries the whole call; only the successful
        attempt's messages reach the sink, never both attempts'."""
        from openai import APITimeoutError

        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                Message(role=Role.ASSISTANT, content="第二次成了"),
            ]
        )
        produced: list[Message] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="醒了")],
                transcript_sink=produced, call=_call(), prompts=_PROMPTS,
            )
        assert [m.text() for m in produced] == ["第二次成了"]
