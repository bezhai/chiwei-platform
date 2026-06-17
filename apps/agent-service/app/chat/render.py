"""Shared chat-turn render layer — persona prompt + main model → text out.

This is the **how** half of a chat turn (decision 2 in the proactive-chat spec):
given an already-built context + persona + outbound params, run the persona
``main`` prompt on the main model, stream the reply, and fire post-actions.

Crucially it has **no source-message dependency**: the only message identity it
takes is ``outbound_message_id``, threaded straight through to the agent context
and post-actions as a trace / outbound tag. The caller decides what that id is —
the real-person path passes the real source message id; the life/proactive path
(task 2) passes its own act-derived id. Building the context (history, persona,
inner-context) belongs to the two context builders that feed this layer:

  - human chat  → ``app.chat.context.build_human_chat_context`` (task 1)
  - life/proactive → its own builder (task 2)

both produce a :class:`ChatTurnContext`, which this layer consumes uniformly.

Public surface:
    ChatTurnContext  — render-ready context carried from a context builder
    render_chat_turn(turn_ctx, *, outbound_message_id, session_id, channel, features)
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.tools import ALL_TOOLS
from app.api.middleware import CHAT_PIPELINE_DURATION, CHAT_TOKENS
from app.chat.post_actions import schedule_post_actions
from app.chat.stream import (
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)
from app.infra.image import ImageRegistry
from app.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

_MAIN_CFG = AgentConfig("main", "main-chat-model", "main")


class RenderFailed(Exception):
    """渲染没产出可发的内容（stream 抛错 / content_filter / length 截断）。

    只在 ``on_error="raise"`` 模式下抛（proactive 用）：proactive 复用真人回复的渲染层，
    但**绝不能把 persona error 文案 / 截断提示当成功内容主动发给真人**（spec 决策：渲染
    失败不出站、把「没发出去」喂回 life，不回退发原文、不发错误文本）。``kind`` 标明哪种
    失败（``error`` / ``content_filter`` / ``length``）供调用方记日志。默认模式
    （``on_error="yield_text"``，真人回复用）语义不变：吞掉这些、yield 错误 / 截断文案
    给用户看（用户在等回复，给一句话比静默好）。
    """

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"render failed: {kind}")


@dataclass
class ChatTurnContext:
    """Render-ready chat context: everything the render layer needs, nothing more.

    Produced by a context builder (human-chat or life/proactive), consumed by
    :func:`render_chat_turn`. Bundles the LLM ``messages``, the image registry,
    the conversation address (``chat_id``), the resolved persona bundle
    (``persona_id`` + ``identity`` + ``appearance`` + the ``persona`` object for
    error messages), and the assembled ``inner_context``.

    No source-message id lives here: the builders already did all source lookups
    and packed the result. The render layer threads only an ``outbound_message_id``
    supplied by its caller.
    """

    messages: list
    image_registry: ImageRegistry | None
    chat_id: str
    persona_id: str
    identity: str
    appearance: str
    inner_context: str
    persona: object | None = None

    def error_message(self, kind: str) -> str:
        """Persona-specific error message, with display-name / id fallback."""
        if self.persona is not None and getattr(self.persona, "error_messages", None):
            return self.persona.error_messages.get(
                kind, f"{getattr(self.persona, 'display_name', self.persona_id)}遇到了问题QAQ"
            )
        return f"{self.persona_id}遇到了问题QAQ"


async def render_chat_turn(
    turn_ctx: ChatTurnContext,
    *,
    outbound_message_id: str,
    session_id: str | None,
    channel: str,
    features: dict | None = None,
    on_error: str = "yield_text",
) -> AsyncGenerator[str, None]:
    """Render one chat turn from an already-built context.

    Assembles ``prompt_vars`` from the context's persona bundle + inner_context
    plus render-time variables (available skills, complexity hint), resolves the
    model (``features['main_model']`` overrides the default ``main-chat-model``),
    runs ``Agent.stream`` on the persona ``main`` prompt, and yields decoded text
    fragments + split markers (the caller segments / emits them).

    ``on_error`` decides what happens on a non-success outcome (stream exception /
    ``content_filter`` / ``length`` truncation) — the two chat sources need opposite
    behaviour and **must not be conflated** (codex 必改 2):

      - ``"yield_text"`` (default, **真人回复路径**): ``content_filter`` → persona
        content_filter message; ``length`` → truncation notice; a stream exception →
        persona error message. All three terminate the generator *without raising*,
        matching the legacy ``_build_and_stream`` behaviour — a real person is waiting
        for a reply, so a one-line error / truncated text beats silence.
      - ``"raise"`` (**proactive 路径**): the same three outcomes :class:`RenderFailed`
        instead of yielding error / truncation text — proactive reuses this layer but
        must never主动 ship "ERR / 遇到了问题 / 截断" to a real person as if it were
        her message. The proactive caller catches it, does **not** emit, and feeds
        「没发出去」back to life (spec: render failure → no outbound, no fall-back to
        the raw life intent, no error text).

    On clean completion it fires ``schedule_post_actions`` (post safety) keyed on
    ``outbound_message_id`` / ``session_id`` / ``chat_id`` — identical in both modes.
    """
    features = features or {}
    raise_on_error = on_error == "raise"

    prompt_vars: dict[str, str] = {
        "complexity_hint": "",
        "inner_context": turn_ctx.inner_context,
        "available_skills": SkillRegistry.list_descriptions(),
        "identity": turn_ctx.identity,
        "appearance": turn_ctx.appearance,
    }

    model_id = "main-chat-model"
    if features.get("main_model"):
        model_id = str(features["main_model"])

    cfg = replace(_MAIN_CFG, model_id=model_id)
    agent = Agent(cfg, tools=ALL_TOOLS)
    state = StreamState()

    t_agent_start = time.monotonic()
    try:
        async for token in agent.stream(
            turn_ctx.messages,
            context=AgentContext(
                message_id=outbound_message_id,
                chat_id=turn_ctx.chat_id,
                persona_id=turn_ctx.persona_id,
                image_registry=turn_ctx.image_registry,
                features=dict(features),
            ),
            prompt_vars=prompt_vars,
        ):
            result = handle_token(token, state)

            if is_content_filter(result):
                if raise_on_error:
                    raise RenderFailed("content_filter")
                yield turn_ctx.error_message("content_filter")
                return
            if is_length_truncated(result):
                if raise_on_error:
                    raise RenderFailed("length")
                yield "(后续内容被截断)"
                return

            for text in result:
                if text is not None:
                    yield text

        agent_dur = time.monotonic() - t_agent_start
        CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(agent_dur)
        CHAT_TOKENS.labels(type="text").inc(state.agent_token_count)
        CHAT_TOKENS.labels(type="tool_call").inc(state.tool_call_count)
        logger.info(
            "agent_stream_done",
            extra={
                "event": "agent_stream_done",
                "session_id": session_id,
                "agent_ms": round(agent_dur * 1000),
                "tokens": state.agent_token_count,
                "tools": state.tool_call_count,
                "model": model_id,
            },
        )

        await schedule_post_actions(
            full_content=state.full_content,
            session_id=session_id,
            channel=channel,
            chat_id=turn_ctx.chat_id,
            message_id=outbound_message_id,
        )

    except RenderFailed:
        # content_filter / length 在 raise 模式下抛出的失败信号：原样上抛给调用方，
        # 绝不在这里被通用 except 收成 persona error 文案（那会让 proactive 又把错误
        # 文本当内容）。日志在抛出点的语义已清楚，这里不重复记。
        raise
    except Exception as e:
        import traceback

        logger.error(
            "render_chat_turn error: %s\n%s", e, traceback.format_exc()
        )
        if raise_on_error:
            # proactive 路径：stream 抛异常 → 抛 RenderFailed，调用方据此不出站、
            # 喂回 life。不 yield persona error 文案（绝不主动发错误文本给真人）。
            raise RenderFailed("error") from e
        yield turn_ctx.error_message("error")
