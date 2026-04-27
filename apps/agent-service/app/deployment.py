"""Node -> PaaS App bindings.

Every ``@node`` not bound here defaults to the main ``agent-service`` app.
App names must already exist in PaaS (create via ``/api/paas/apps/``
before binding, otherwise the deploy step has nowhere to land).

The whole message pipeline lives on the ``vectorize-worker`` Deployment —
``hydrate_message`` consumes the MQ entry, ``vectorize`` does embedding,
``save_fragment`` writes qdrant. Keeping them co-located avoids an extra
RabbitMQ hop between vectorize and save_fragment.

Memory v4 vectorize @nodes (``vectorize_memory_fragment`` /
``vectorize_memory_abstract``) also live on ``vectorize-worker`` — they
read from pg + write to qdrant, same I/O profile as ``vectorize`` and
``save_fragment``, so co-locating avoids spinning up another worker
deployment just for this lane.
"""
from app.nodes.hydrate_message import hydrate_message
from app.nodes.memory_vectorize import (
    vectorize_memory_abstract,
    vectorize_memory_fragment,
)
from app.nodes.save_fragment import save_fragment
from app.nodes.vectorize import vectorize
from app.runtime import bind

bind(hydrate_message).to_app("vectorize-worker")
bind(vectorize).to_app("vectorize-worker")
bind(save_fragment).to_app("vectorize-worker")
bind(vectorize_memory_fragment).to_app("vectorize-worker")
bind(vectorize_memory_abstract).to_app("vectorize-worker")
