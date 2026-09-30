"""world App 的接线：只有 world 的进程 import 它（``app.deployment.APP_WIRING``）。

world 靠收件箱醒：开设名为 ``world`` 的收件箱，一次只处理一条、一轮最多
:data:`app.world.main_agent.ROUND_TIMEOUT`（占位租约随之放长），开设时按私有状态补醒
（:func:`app.world.wake.wake_on_start`）。它不接受提问——回答"某处现在什么样"的应答
agent 还没有。
"""
from app.messaging.receiving import inbox
from app.world.main_agent import ROUND_TIMEOUT, on_world_message
from app.world.wake import WORLD, wake_on_start

inbox(
    WORLD,
    on_message=on_world_message,
    processing_timeout=ROUND_TIMEOUT,
    one_at_a_time=True,
    on_open=wake_on_start,
)
