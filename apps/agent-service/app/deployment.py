"""Node -> PaaS App bindings.

**现在一条绑定都没有。** 每个 ``@node`` 都 fall through 到默认 app
``agent-service``——living 引擎的节点全跑在主进程里，没有第二个 app。

（v4 记忆向量化的 vectorize-worker 绑定随旧记忆机器整体删除；旧 chat 的
``persist_tos_files_node`` 与 pre/post 安全检查那两个节点随旧实现整体删除。三个
app 名都已无任何节点，Deployment 下线属运维动作。）

要把某个节点挪到别的 app，在这里 ``bind(node).to_app("name")``。App 名必须已经
存在于 PaaS（先 ``/api/paas/apps/`` 建，否则部署那步没有落脚处）。
"""
