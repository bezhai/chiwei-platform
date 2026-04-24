"""Message-pipeline wiring: MQ entry -> hydrate -> vectorize -> save.

Two entry points, one pipeline:
  * ``Source.mq("vectorize")`` feeds ``hydrate_message`` — legacy publishers
    (lark-server today) keep posting ``{"message_id": X}`` frames to the
    old queue; the engine decodes them into ``MessageRequest`` and invokes
    ``hydrate_message`` which pulls the real row and emits a ``Message``.
  * ``emit_legacy_message(cm)`` (inside proactive.py and other Python-side
    ConversationMessage writers) lifts the row directly into a ``Message``
    via the in-process emit, bypassing the queue.

Both paths converge on the durable ``Message`` wire, so ``vectorize``
receives messages from either source indistinguishably. ``Fragment`` is
transient: ``vectorize`` -> ``save_fragment`` is an in-process edge
within the same vectorize-worker pod.
"""
from app.domain.fragment import Fragment
from app.domain.message import Message
from app.domain.message_request import MessageRequest
from app.nodes.hydrate_message import hydrate_message
from app.nodes.save_fragment import save_fragment
from app.nodes.vectorize import vectorize
from app.runtime.source import Source
from app.runtime.wire import wire

# MQ entry: lark-server publishes {"message_id": X} to the "vectorize" queue.
wire(MessageRequest).to(hydrate_message).from_(Source.mq("vectorize"))

# Message durable -> vectorize (both entry paths converge here).
wire(Message).to(vectorize).durable()

# Fragment -> save_fragment (in-process within vectorize-worker).
wire(Fragment).to(save_fragment)
