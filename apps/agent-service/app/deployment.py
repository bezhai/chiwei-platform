"""这个镜像跑出哪几个 App，每个 App 的进程起哪些插件（:data:`APPS`）。

**一个 App 的进程只 import 它自己清单里的插件。** 同一个镜像由 PaaS 部署成几个 App（PaaS 给
每个 Deployment 注入 ``APP_NAME``），``app.main`` 的 lifespan 按 :data:`APPS` 起那一个 App 的
插件宿主（:meth:`app.host.Host.for_app`）。插件模块 import 到的东西（Data 类、节点）和插件
setup 里登记的东西（钟、路由、收件箱）才会出现在那个进程里；没被 import 的代码在那个进程里
不存在——不建它的表、不跑它的钟、不开它的收件箱。

* ``agent-service``：运维接口、通信机制的人工入口、guides、living 引擎（钟、读书、出站、
  三姐妹的收件箱）。
* ``world``：world 的知识来源、收件箱和记录的人工读写接口。

App 之间不靠代码互通，靠通信机制（:mod:`app.messaging`）。App 名必须已经存在于 PaaS（先
``/api/paas/apps/`` 建，否则部署那步没有落脚处）。
"""
from __future__ import annotations

DEFAULT_APP = "agent-service"
"""没设 ``APP_NAME`` 的进程跑的 App。"""

APPS: dict[str, tuple[str, ...]] = {
    "agent-service": (
        "app.plugins.ops",
        "app.plugins.operator",
        "app.plugins.skills",
        "app.plugins.living",
    ),
    "world": ("app.plugins.world",),
}
"""每个 App 的插件清单：插件模块名（各自暴露一个 ``PLUGIN``），:meth:`app.host.Host.for_app` 按它
建宿主。写成字符串、到 ``for_app`` 才 import，所以一个 App 的进程只加载它清单里的插件——world
的进程不加载 living 的代码。
"""
