"""Memory v4 vectorize wiring: per-row MQ entries -> embed + qdrant.

Two independent MQ entry points feed the same vectorize-worker pod:

  * ``Source.mq("memory_fragment_vectorize")`` — body
    ``{"fragment_id": "f_xxx"}``, decoded into ``MemoryFragmentRequest``,
    consumed by ``vectorize_memory_fragment``.
  * ``Source.mq("memory_abstract_vectorize")`` — body
    ``{"abstract_id": "a_xxx"}``, decoded into ``MemoryAbstractRequest``,
    consumed by ``vectorize_memory_abstract``.

Two queues instead of one tagged frame keep the ``Source.mq`` contract
honest: a single queue only maps to one Data type today (compile_graph
layer 3a), and splitting Fragment vs Abstract avoids smuggling a ``kind``
discriminator into a Data payload that should describe one row only.

Publishers stay outside the graph (``app.memory.vectorize_memory``'s
``enqueue_*`` helpers call ``mq.publish`` directly): the Data classes
are ``Meta.transient = True`` so they have no pg table, and durable
edges aren't applicable to an MQ-source ingress edge.
"""
from app.domain.memory_request import MemoryAbstractRequest, MemoryFragmentRequest
from app.nodes.memory_vectorize import (
    vectorize_memory_abstract,
    vectorize_memory_fragment,
)
from app.runtime import Source, wire

wire(MemoryFragmentRequest).to(vectorize_memory_fragment).from_(
    Source.mq("memory_fragment_vectorize")
)
wire(MemoryAbstractRequest).to(vectorize_memory_abstract).from_(
    Source.mq("memory_abstract_vectorize")
)
