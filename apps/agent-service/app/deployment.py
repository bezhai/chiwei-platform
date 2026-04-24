"""Node -> PaaS App bindings.

Every ``@node`` not bound here defaults to the main ``agent-service`` app.
App names must already exist in PaaS (create via ``/api/paas/apps/``
before binding, otherwise the deploy step has nowhere to land).

The whole message pipeline lives on the ``vectorize-worker`` Deployment —
``hydrate_message`` consumes the MQ entry, ``vectorize`` does embedding,
``save_fragment`` writes qdrant. Keeping them co-located avoids an extra
RabbitMQ hop between vectorize and save_fragment.
"""
from app.nodes.hydrate_message import hydrate_message
from app.nodes.save_fragment import save_fragment
from app.nodes.vectorize import vectorize
from app.runtime.placement import bind

bind(hydrate_message).to_app("vectorize-worker")
bind(vectorize).to_app("vectorize-worker")
bind(save_fragment).to_app("vectorize-worker")
