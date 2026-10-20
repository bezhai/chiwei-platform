"""world 的清单：只有 world 插件，所以 world 的进程不加载 living 的任何代码。"""

from __future__ import annotations

from app.host.manifest import App

MANIFEST = App(
    plugins=("app.plugins.world",),
    agents={},
    profiles={},
    participants=(),
)
