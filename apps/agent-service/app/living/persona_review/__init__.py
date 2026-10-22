"""persona_review 插件：她每周的回看 —— 周一早上她回头看上一周别人对她说了什么、她身边发生了什么，
重写一版「我是谁」进版本链（周一没写成，同一周后面几天的早上再试）。锚是 ``bot_persona.persona_core``
那个扁平列，不给锚她就是在自我回流（:mod:`app.living.persona_review.review`）。

  * 三姐妹各一条回看队列（``persona-review``），各挂一个回看的唤醒：钟每五分钟一拍，那一拍落在
    06:00–08:00 的窗口里、这一周还没回看过，就回看上一个完整自然周
    （:func:`app.living.persona_review.review.persona_review_due`）。
  * 要 ``persona`` 服务（她本来是谁、这一周写过没有、新的一版写进哪条链）、``phone`` 服务（那一周别人在
    手机上对她说的）和 ``received`` 服务（那一周传到她这里的：姐妹当面说的、world 告诉她察觉到的）。

**回看有自己的一条队列**，跟她的轮次、日记各占各的，占用上限相同（:data:`app.living.common.HOLD_LIMIT`），
钟的每一拍都排队，轮到时再判（同 :mod:`app.living.day_page`）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial

from app.host import Batch, Coalesce, Context, Plugin, WakeSource
from app.living.common import HOLD_LIMIT, living_lane
from app.living.persona_review.review import (
    DueReview,
    persona_review_due,
    write_due_review,
)
from app.living.services import Persona, PersonaText, Phone, Received

# 五分钟一拍：窗口两小时里试 24 次；没写成的那一拍由下一拍再试。
PERSONA_REVIEW_EVERY = timedelta(minutes=5)


def persona_review_wake(
    lane: str, persona: Persona, phone: Phone, received: Received
) -> WakeSource[DueReview, PersonaText | None]:
    """``lane`` 上她每周的回看：那一拍落在窗口里、这一周还没回看过，就从上一周别人对她说的（``phone``
    服务）和传到她这里的（``received`` 服务）写一版新的「我是谁」，写进 ``persona`` 服务的那条链。"""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DueReview | None:
        (tick,) = batch.close()
        return await persona_review_due(
            lane=lane,
            persona_id=persona_id,
            at=tick.at,
            clock=clock,
            persona=persona,
            phone=phone,
            received=received,
        )

    return WakeSource(
        "persona-review", Coalesce.QUEUE_EACH, due, partial(write_due_review, persona=persona)
    )


def setup(ctx: Context) -> None:
    reviews = persona_review_wake(
        living_lane(), ctx.service("persona"), ctx.service("phone"), ctx.service("received")
    )
    for persona_id in ctx.participants(profile="resident"):
        looked_back_in = ctx.turns(persona_id, "persona-review", hold_limit=HOLD_LIMIT)
        ctx.wake(looked_back_in, reviews, every=PERSONA_REVIEW_EVERY)


PLUGIN = Plugin(
    name="persona_review",
    setup=setup,
    requires=("persona", "phone", "received"),
)
