"""agent-service 这个 App 的接线：import 时每个子模块的 ``wire(...)`` / ``inbox(...)`` 生效。

只有 agent-service 的进程 import 这个包（见 ``app.deployment.APP_WIRING``）。
"""

from app.wiring import (  # noqa: F401
    admin,
    living,
    messaging,
    safety,
)
