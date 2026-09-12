"""Provider-agnostic types exchanged between the thinking core and adapters.

These replace ``langchain_core.messages`` / langchain tool + chunk types. They
are intentionally *neutral*: no provider's wire layout is baked in. Each
adapter (OpenAI in T2, Gemini in T4) owns the translation ``neutral ↔ wire``.

The field coverage here is a behaviour-equivalence hard constraint, not an
implementation detail (spec §Key design decisions). It must carry, without
loss:

  - multimodal content blocks: chat-history images (``{"type":"image",
    "url":...}`` from build_*_messages) and OpenAI-style ``image_url`` blocks
    returned by tools,
  - the full sequence one model turn came back as (:class:`TurnPart`): its
    thoughts, its spoken text, its tool calls, in the order they arrived and
    each with its own provider signature,
  - string-content normalisation (deepseek rejects array / null content),
  - tool_call (assistant requests a tool) + tool_result (the tool's answer).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# ---------------------------------------------------------------------------
# Content blocks (multimodal)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContentBlock:
    """One piece of multimodal content.

    ``type`` is the discriminator. The carried fields are kept neutral; a
    block only populates the field its ``type`` needs:

      - ``text``      -> ``text``
      - ``image``     -> ``url`` (chat-history image, build_*_messages shape)
      - ``image_url`` -> ``image_url`` (OpenAI-style block returned by a tool)

    Construct via the ``from_*`` factories below rather than positionally.
    """

    type: str
    text: str | None = None
    url: str | None = None
    image_url: dict[str, Any] | None = None

    @classmethod
    def from_text(cls, text: str) -> ContentBlock:
        return cls(type="text", text=text)

    @classmethod
    def from_image(cls, *, url: str) -> ContentBlock:
        return cls(type="image", url=url)

    @classmethod
    def from_image_url(cls, image_url: dict[str, Any]) -> ContentBlock:
        return cls(type="image_url", image_url=image_url)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            out["text"] = self.text
        if self.url is not None:
            out["url"] = self.url
        if self.image_url is not None:
            out["image_url"] = self.image_url
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContentBlock:
        return cls(
            type=d["type"],
            text=d.get("text"),
            url=d.get("url"),
            image_url=d.get("image_url"),
        )


def normalize_content_to_text(content: str | list[ContentBlock] | None) -> str:
    """Flatten content to a plain string.

    DeepSeek rejects null / array content, so its adapter normalises every
    message down to text. Image blocks contribute no text. Mirrors the legacy
    ``_ReasoningChatOpenAI._normalize_content`` behaviour but on neutral types.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        block.text or "" for block in content if block.type == "text"
    )


# ---------------------------------------------------------------------------
# Tool call / result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolDef:
    """A tool the model may call — name + description + JSON-schema params."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    """The model's request to invoke a tool.

    ``signature`` is an opaque, provider-specific blob the model attaches to a
    tool call and demands back verbatim on the next turn. Gemini 2.5 thinking
    models put a ``thought_signature`` on the functionCall part and reject the
    following turn with 400 INVALID_ARGUMENT if it isn't echoed. It is carried
    in memory through the ReAct loop only (bytes aren't JSON-serialisable), so
    ``to_dict`` / ``from_dict`` deliberately omit it — those serve langfuse
    tracing, not the wire.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    signature: bytes | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolCall:
        return cls(
            id=d["id"], name=d["name"], arguments=d.get("arguments") or {}
        )

    def to_replay_dict(self) -> dict[str, Any]:
        """JSON-safe dict that survives a *lossless* round-trip for replay.

        Unlike ``to_dict`` (which serves langfuse and deliberately drops the
        opaque ``signature``), this keeps the provider-private blob so a stored
        transcript can be fed back to the model verbatim. ``signature`` is raw
        bytes — not JSON-serialisable — so it is base64-encoded under a
        ``signature_b64`` key. Used by the session store (decision 4).
        """
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.signature is not None:
            out["signature_b64"] = base64.b64encode(self.signature).decode("ascii")
        return out

    @classmethod
    def from_replay_dict(cls, d: dict[str, Any]) -> ToolCall:
        sig_b64 = d.get("signature_b64")
        return cls(
            id=d["id"],
            name=d["name"],
            arguments=d.get("arguments") or {},
            signature=base64.b64decode(sig_b64) if sig_b64 is not None else None,
        )


@dataclass(slots=True)
class ToolResult:
    """The result a tool produced, addressed back to its ToolCall."""

    tool_call_id: str
    content: str | list[ContentBlock]

    def to_message(self) -> Message:
        return Message(
            role=Role.TOOL,
            content=self.content,
            tool_call_id=self.tool_call_id,
        )


# ---------------------------------------------------------------------------
# Model turn sequence
# ---------------------------------------------------------------------------


class TurnPartKind(StrEnum):
    TEXT = "text"
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"


@dataclass(slots=True)
class TurnPart:
    """One segment of what a model returned in a single turn.

    A turn is a *sequence*: the model can return a thought, some text, a tool
    call, another thought and another call, and each segment can carry its own
    opaque ``signature`` (gemini's ``thought_signature``). Providers running
    stateless — we hold the transcript ourselves and resend it every turn —
    require that whole sequence back unaltered, signatures included, so the
    boundaries, the order and the signature-to-segment mapping all have to
    survive storage.

    ``text`` carries the payload of a ``TEXT`` / ``THOUGHT`` part. A
    ``TOOL_CALL`` part holds only ``call_id``: the call itself lives once, in
    ``Message.tool_calls`` (with its own signature), and this part records where
    in the sequence it sat.
    """

    kind: TurnPartKind
    text: str = ""
    signature: bytes | None = None
    call_id: str | None = None

    @classmethod
    def from_text(cls, text: str, *, signature: bytes | None = None) -> TurnPart:
        return cls(kind=TurnPartKind.TEXT, text=text, signature=signature)

    @classmethod
    def from_thought(cls, text: str, *, signature: bytes | None = None) -> TurnPart:
        return cls(kind=TurnPartKind.THOUGHT, text=text, signature=signature)

    @classmethod
    def from_tool_call(cls, call: ToolCall) -> TurnPart:
        return cls(kind=TurnPartKind.TOOL_CALL, call_id=call.id)

    def to_dict(self) -> dict[str, Any]:
        """Langfuse-facing dict: the signature by size, never its bytes.

        Same rule as every other opaque blob on a trace (a picture renders as
        mime + length): the bytes would bury the conversation, while the size is
        enough to answer "did a signature ride along".
        """
        out: dict[str, Any] = {"kind": str(self.kind)}
        if self.text:
            out["text"] = self.text
        if self.call_id is not None:
            out["call_id"] = self.call_id
        if self.signature is not None:
            out["signature_bytes"] = len(self.signature)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TurnPart:
        return cls(
            kind=TurnPartKind(d["kind"]),
            text=d.get("text", ""),
            call_id=d.get("call_id"),
        )

    def to_replay_dict(self) -> dict[str, Any]:
        """Lossless JSON-safe dict — the signature rides as base64."""
        out: dict[str, Any] = {"kind": str(self.kind)}
        if self.text:
            out["text"] = self.text
        if self.call_id is not None:
            out["call_id"] = self.call_id
        if self.signature is not None:
            out["signature_b64"] = base64.b64encode(self.signature).decode("ascii")
        return out

    @classmethod
    def from_replay_dict(cls, d: dict[str, Any]) -> TurnPart:
        sig_b64 = d.get("signature_b64")
        return cls(
            kind=TurnPartKind(d["kind"]),
            text=d.get("text", ""),
            signature=base64.b64decode(sig_b64) if sig_b64 is not None else None,
            call_id=d.get("call_id"),
        )


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Message:
    """One conversation turn, provider-agnostic.

    ``content`` is either a plain string or a list of content blocks (for
    multimodal). Assistant turns additionally carry ``tool_calls``. Tool turns
    carry ``tool_call_id`` linking back to the assistant's request.

    ``turn_parts`` is set on the turns an adapter built from a model response:
    it is the complete sequence that response came back as (:class:`TurnPart`),
    and an adapter that finds it uses it verbatim to rebuild the turn on the
    wire. ``content`` and ``tool_calls`` stay the views every other layer reads
    ("what she said", "what she called"); :meth:`from_model_turn` derives them
    from the sequence so there is one place the two can be built. Empty
    ``turn_parts`` means nobody recorded a sequence for this message (a SYSTEM /
    USER / TOOL turn, a turn read back from an older transcript), and the wire
    is then rebuilt from ``content`` + ``tool_calls``.
    """

    role: Role
    content: str | list[ContentBlock] = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    turn_parts: list[TurnPart] = field(default_factory=list)

    @classmethod
    def from_model_turn(
        cls, parts: list[TurnPart], tool_calls: list[ToolCall]
    ) -> Message:
        """An assistant turn built from the sequence a model returned.

        ``content`` is the concatenation of the text parts — the spoken text,
        thoughts excluded, which is what ``text()`` readers expect — and
        ``tool_calls`` are the calls the ``TOOL_CALL`` parts point at.
        """
        return cls(
            role=Role.ASSISTANT,
            content="".join(
                p.text for p in parts if p.kind is TurnPartKind.TEXT
            ),
            tool_calls=list(tool_calls),
            turn_parts=list(parts),
        )

    def text(self) -> str:
        """Normalised plain-text view of ``content``."""
        return normalize_content_to_text(self.content)

    def thought_text(self) -> str:
        """Everything the model thought this turn, concatenated."""
        return "".join(
            p.text for p in self.turn_parts if p.kind is TurnPartKind.THOUGHT
        )

    def to_dict(self) -> dict[str, Any]:
        if isinstance(self.content, list):
            content: Any = [b.to_dict() for b in self.content]
        else:
            content = self.content
        out: dict[str, Any] = {"role": str(self.role), "content": content}
        if self.tool_calls:
            out["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.turn_parts:
            out["turn_parts"] = [p.to_dict() for p in self.turn_parts]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        raw_content = d.get("content", "")
        content: str | list[ContentBlock]
        if isinstance(raw_content, list):
            content = [ContentBlock.from_dict(b) for b in raw_content]
        else:
            content = raw_content
        return cls(
            role=Role(d["role"]),
            content=content,
            tool_calls=[
                ToolCall.from_dict(tc) for tc in d.get("tool_calls", [])
            ],
            tool_call_id=d.get("tool_call_id"),
            turn_parts=[TurnPart.from_dict(p) for p in d.get("turn_parts", [])],
        )

    def to_replay_dict(self) -> dict[str, Any]:
        """Lossless JSON-safe dict for storing a replay-able transcript.

        Same shape as ``to_dict`` except tool calls and turn parts use their
        lossless ``to_replay_dict`` so provider-private blobs (gemini
        ``thought_signature``) survive serialise → PG → deserialise → model.
        """
        if isinstance(self.content, list):
            content: Any = [b.to_dict() for b in self.content]
        else:
            content = self.content
        out: dict[str, Any] = {"role": str(self.role), "content": content}
        if self.tool_calls:
            out["tool_calls"] = [tc.to_replay_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.turn_parts:
            out["turn_parts"] = [p.to_replay_dict() for p in self.turn_parts]
        return out

    @classmethod
    def from_replay_dict(cls, d: dict[str, Any]) -> Message:
        raw_content = d.get("content", "")
        content: str | list[ContentBlock]
        if isinstance(raw_content, list):
            content = [ContentBlock.from_dict(b) for b in raw_content]
        else:
            content = raw_content
        return cls(
            role=Role(d["role"]),
            content=content,
            tool_calls=[
                ToolCall.from_replay_dict(tc) for tc in d.get("tool_calls", [])
            ],
            tool_call_id=d.get("tool_call_id"),
            turn_parts=[
                TurnPart.from_replay_dict(p) for p in d.get("turn_parts", [])
            ],
        )


# ---------------------------------------------------------------------------
# Stream chunk
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StreamChunk:
    """One increment of a streaming response.

    Carries everything ``app/chat/stream.py`` consumes: a text token, a
    ``finish_reason`` signal (``content_filter`` / ``length`` / ``stop`` /
    ``tool_calls``), a tool_call (the text→tool boundary marker), a
    tool_result, and reasoning passthrough. Any field may be ``None``; a chunk
    typically populates exactly one.

    ``signature`` is the opaque blob the provider attached to the part this
    chunk came from (a thought or a text part). The ReAct loop reassembles the
    streamed turn into a :class:`Message`, and without a slot here the
    signatures of a streamed turn would be gone before that happens — a tool
    call carries its own on :attr:`ToolCall.signature`.

    ``starts_part`` says this chunk opens a new :class:`TurnPart`. Only the
    adapter can know that: it reads the provider's parts, and two parts that
    arrived inside one wire chunk are two parts however their text and
    signatures look. ``False`` means the boundary is unknown — the usual case,
    one part cut across many chunks — and the loop then extends the part still
    open.
    """

    text: str | None = None
    reasoning: str | None = None
    finish_reason: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    signature: bytes | None = None
    starts_part: bool = False
