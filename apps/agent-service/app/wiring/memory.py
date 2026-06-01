"""Message-pipeline wiring: MQ entry -> hydrate -> vectorize -> save.

Two entry points, one pipeline:
  * ``Source.mq("vectorize")`` feeds ``hydrate_message``. Publishers post
    ``{"message_id": X}`` frames where X is a common message id.
  * ``proactive.py`` inserts ``common_message`` and emits ``MessageRequest``;
    because ``hydrate_message`` is bound to ``vectorize-worker`` and the wire
    declares ``Source.mq("vectorize")``, runtime emit publishes to that queue.

Both paths converge at ``hydrate_message``. ``Message`` and ``Fragment`` are
transient in-process data inside the vectorize-worker pod.
"""
from app.domain.fragment import Fragment
from app.domain.message import Message
from app.domain.message_request import MessageRequest
from app.nodes.hydrate_message import hydrate_message
from app.nodes.save_fragment import save_fragment
from app.nodes.vectorize import vectorize
from app.runtime import Source, wire

# MQ entry: channel-server publishes {"message_id": X} to the "vectorize" queue.
wire(MessageRequest).to(hydrate_message).from_(Source.mq("vectorize"))

# Message -> vectorize (in-process inside vectorize-worker).
wire(Message).to(vectorize)

# Fragment -> save_fragment (in-process within vectorize-worker).
wire(Fragment).to(save_fragment)
