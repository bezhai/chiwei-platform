"""Node -> PaaS App bindings.

Every ``@node`` not bound here defaults to the main ``agent-service`` app.
App names must already exist in PaaS (create via ``/api/paas/apps/``
before binding, otherwise the deploy step has nowhere to land).

（v4 记忆向量化的 vectorize-worker 绑定随旧记忆机器整体删除；该 app 已无
任何节点，Deployment 下线属运维动作。）
"""
from app.nodes.persist_tos_files import persist_tos_files_node
from app.runtime import bind

# Phase 6 v4 Gap 5: durable consumer for CommonMessageContentSynced runs
# in the agent-service main process — matches the old asyncio.create_task
# placement (chat handler co-located DB write).
bind(persist_tos_files_node).to_app("agent-service")
