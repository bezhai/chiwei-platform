"""Node -> PaaS App bindings.

Every ``@node`` not bound here defaults to the main ``agent-service`` app.
App names must already exist in PaaS (create via ``/api/paas/apps/``
before binding, otherwise the deploy step has nowhere to land).

Memory v4 vectorize @nodes (``vectorize_memory_fragment`` /
``vectorize_memory_abstract``) live on ``vectorize-worker`` — they
read from pg + write to qdrant, so co-locating avoids spinning up another
worker deployment just for this lane.
"""
from app.nodes.memory_vectorize import (
    vectorize_memory_abstract,
    vectorize_memory_fragment,
)
from app.nodes.persist_tos_files import persist_tos_files_node
from app.runtime import bind

bind(vectorize_memory_fragment).to_app("vectorize-worker")
bind(vectorize_memory_abstract).to_app("vectorize-worker")

# Phase 6 v4 Gap 5: durable consumer for CommonMessageContentSynced runs
# in the agent-service main process — matches the old asyncio.create_task
# placement (chat handler co-located DB write).
bind(persist_tos_files_node).to_app("agent-service")
