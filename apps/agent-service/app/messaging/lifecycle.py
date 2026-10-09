"""进程启停时通信机制要做的事。插件宿主（:mod:`app.host`）启停时调这里。

启动：连 broker、确保主交换机在；声明本泳道的定时队列并开始消费；开设本 App 声明的
全部收件箱（见 :func:`app.messaging.receiving.inbox`）。每个跑着通信机制的进程都消费
本泳道的定时队列——同一泳道里有几个 App 就有几个消费者，谁拿到谁送。

停止：取消全部消费者，关掉提问用的回复队列，让还在等回答的提问立刻拿到"没有回答"。
"""
from __future__ import annotations

from app.infra.rabbitmq import mq
from app.messaging.receiving import start_receiving, stop_receiving
from app.messaging.sending import close_replies


async def start_messaging() -> None:
    await mq.connect()
    await mq.declare_topology()
    await start_receiving()


async def stop_messaging() -> None:
    await stop_receiving()
    await close_replies()
