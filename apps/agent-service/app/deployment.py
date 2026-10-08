"""这个镜像跑出哪几个 App，每个 App 的进程加载哪些接线模块；以及节点 -> App 的绑定。

**一个 App 的进程只 import 它自己的接线。** 同一个镜像由 PaaS 部署成几个 App（PaaS 给
每个 Deployment 注入 ``APP_NAME``），``app.runtime.bootstrap.load_dataflow_graph`` 按
这里的 :data:`APP_WIRING` 只加载那一个 App 的接线模块。接线模块 import 到的东西（Data
类、节点、钟、收件箱）才会出现在那个进程里；没被 import 的代码在那个进程里不存在——
不建它的表、不跑它的钟、不开它的收件箱。

* ``agent-service``：``app.wiring``（living 引擎的钟和出站、运维 HTTP、通信机制的人工入口）。
* ``world``：``app.world.wiring``（world 的收件箱和记录的人工读写接口）。它不在
  ``app.wiring`` 这个包里：import ``app.wiring.xxx`` 会先执行 ``app/wiring/__init__.py``，
  把 agent-service 的全部接线（连同 life）带进来。

App 之间不靠接线互通，靠通信机制（:mod:`app.messaging`）。

**节点绑定写在各 App 自己的接线模块里。** 没有显式 ``bind`` 的 ``@node`` 落在默认 App
``agent-service`` 上；world 记录的人工读写接口那四个节点在 :mod:`app.world.wiring` 里
``bind(node).to_app("world")``。写在这里的话，每个 App 的进程都会 import 那个节点。
App 名必须已经存在于 PaaS（先 ``/api/paas/apps/`` 建，否则部署那步没有落脚处）。
"""
from __future__ import annotations

APP_WIRING: dict[str, tuple[str, ...]] = {
    "agent-service": ("app.wiring",),
    "world": ("app.world.wiring",),
}

APPS: dict[str, tuple[str, ...]] = {
    "agent-service": (),
    "world": (),
}
"""每个 App 的插件清单：插件模块名（各自暴露一个 ``PLUGIN``），:meth:`app.host.Host.for_app` 按它
建宿主。写成字符串、到 ``for_app`` 才 import，所以一个 App 的进程只加载它清单里的插件——world
的进程不加载 living 的代码。两项由下两个提交填上（agent-service 拆成四个插件，world 一个）；
填上之前进程仍然按上面的 :data:`APP_WIRING` 启动，这里还没有调用方。
"""
