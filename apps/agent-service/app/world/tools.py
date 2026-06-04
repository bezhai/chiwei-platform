"""world 的 agent 工具集 — Task 2（agent 工具循环）.

world 不再"填一张表"返回结构化对象，而是在一个 agent 循环里**连续调工具去行动**：

  * :func:`move_persona` —— 把某 persona 挪到某房间（包 ``set_presence``）。
  * :func:`emit_event`  —— 在某房间产生一条客观动静，投给当时在场者（包
    ``personas_in_room`` + ``deliver_event``，异步 fire-and-forget）。
  * :func:`sleep`       —— 决定下次多久再看一眼世界。它不直接 ``emit_delayed``，
    而是把待办 self-wake 记进本轮 round-scoped state（一轮内多次 sleep 覆盖而非
    追加，最后一次为准），由 engine 在循环收口后 emit 至多一条 self ``WorldTick``
    （避免多次 sleep 叠加未来唤醒风暴）。这是 world 唯一的自排手段。

工具体从 ambient :class:`~app.agent.context.AgentContext` 读 world 本轮的
``lane`` 和 ``round_id``（塞在 ``features`` 里）——lane / round 是机制层的事，
不是世界内容决策，所以不放进工具签名让模型填。``round_id`` 是本轮唤醒的确定性
标识，:func:`emit_event` 用它把 event_id 派生成确定的（lane + room + summary +
round_id），整轮重放时同一条 event 落同一 id、靠 ``deliver_event`` 幂等去重不
重复（决策 3 的幂等命门）。

赤尾设计宪法：
  * :func:`emit_event` 锚定房间、只投给当时在场者（产生侧在场过滤，信息差命门）。
  * summary 是客观可感形态、绝不写情绪 / 主观解读（这条在 prompt 层钉、工具只
    忠实落模型给的文字）。
  * :func:`sleep` 的 1h 上限是"别睡死"的机制边界（决定何时醒），不是用阈值替
    "产什么"的决策。超限**返回错误喂回模型让它重调**，绝不静默夹。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import get_or_create_counter, tool_error
from app.data.queries.mailbox import deliver_event
from app.domain.world_events import EVENT_KIND_AMBIENT
from app.runtime.emit import emit_delayed  # module-level so tests can monkeypatch
from app.world.state import personas_in_room, set_presence

logger = logging.getLogger(__name__)

# sleep 上限：world 最长睡 1h（决策 4）。超限报错喂回模型，绝不静默夹。这是
# "别睡死"的机制边界，不进世界内容决策（赤尾宪法）。
WORLD_SLEEP_MAX_SECONDS = 3600

# 一轮 emit 条数安全阀：正常够不着，触及要 log（不静默截断）—— 决策 4。
WORLD_EMIT_SOFT_CAP = 30

# round-scoped 可变 state 的 features key（engine 每轮新建、工具体跨调用读写、
# engine 收口后读取）。emit 计数撑安全阀（决策 4：触顶 log + 收口）；待办
# self-wake 让一轮内多次 sleep 覆盖而非累积（决策 4：唤醒风暴命门，最后一次为准）。
FEATURE_EMIT_COUNT = "world_emit_count"   # {"n": int} —— 本轮已 emit 计数
FEATURE_SELF_WAKE = "world_self_wake"     # {} | {"delay_ms": int} —— 本轮待办 self-wake

# event_id 派生命名空间：固定 UUID，让 uuid5 在同一 (lane, room, summary, round)
# 上稳定可复现（整轮重放幂等命门）。
_EVENT_ID_NS = uuid.UUID("6f1c8b2a-5e7d-4c3a-9f0b-1d2e3a4b5c6d")

WORLD_EMIT_EVENTS = get_or_create_counter(
    "world_emit_events_total", "world emit_event 投递的 event 计数", ["status"]
)
WORLD_SLEEP_REJECTED = get_or_create_counter(
    "world_sleep_rejected_total", "world sleep 超 1h 上限被拒次数", []
)


def derive_event_id(*, lane: str, room_id: str, summary: str, round_id: str) -> str:
    """确定性派生一条 event 的 id：同 (lane, room, summary, round_id) 同 id。

    整轮重放时模型重复调 emit_event 同一条动静，派生出同一 event_id，
    ``deliver_event`` 按 (lane, persona, event_id) 幂等去重，不会重复投递
    （决策 3）。不同 summary（不同的事）派生不同 id。
    """
    return uuid.uuid5(_EVENT_ID_NS, f"{lane}\x1f{room_id}\x1f{summary}\x1f{round_id}").hex


def _world_round() -> tuple[str, str]:
    """从 ambient context 取 world 本轮的 (lane, round_id)。

    world_tick 在跑 agent 循环前把 lane + round_id 塞进 ``AgentContext.features``，
    工具体在这里读出来行动。没绑 context 直接 ``LookupError`` 失败快，暴露漏了
    ``agent_context(...)`` 的 wiring bug。
    """
    ctx = get_context()
    lane = ctx.features.get("world_lane", "")
    round_id = ctx.features.get("world_round_id", "")
    return lane, round_id


def _emit_count() -> dict:
    """本轮 emit 计数容器 ``{"n": int}``（engine 每轮新建）。

    没有就退回一个临时容器（不该发生：engine 一定会种；防御性兜底不让安全阀
    因 wiring 漏种而炸）。
    """
    return get_context().features.setdefault(FEATURE_EMIT_COUNT, {"n": 0})


def _self_wake_slot() -> dict:
    """本轮待办 self-wake 容器 ``{} | {"delay_ms": int}``（engine 每轮新建）。

    sleep 往里写 delay_ms（覆盖而非追加 → 一轮内多次 sleep 最后一次为准），
    engine 在循环收口后读它 emit 至多一条 self WorldTick。
    """
    return get_context().features.setdefault(FEATURE_SELF_WAKE, {})


@tool
@tool_error("移动失败")
async def move_persona(persona_id: str, room_id: str) -> str:
    """把某个角色挪到某个房间（维护客观在场）。

    到点该上学 / 放学 / 吃饭这类节律边界，或裁准某人的意图要去某处时，用这个
    工具把她挪过去。room_id 用你看到的同一套房间命名。

    Args:
        persona_id: 要移动的角色 id（chinagi / akao / ayana）。
        room_id: 把她挪到哪个房间。

    Returns:
        一句确认文本。
    """
    lane, _round_id = _world_round()
    await set_presence(lane=lane, persona_id=persona_id, room_id=room_id)
    return f"已把 {persona_id} 挪到 {room_id}"


@tool
@tool_error("产生动静失败")
async def emit_event(room_id: str, summary: str) -> str:
    """在某个房间产生一条客观可感的动静，投给当时在那个房间的人。

    summary 必须是客观可感的形态（感官投影），比如"厨房飘来煎蛋和咖啡的香味"
    "玄关传来开关门的声音""晌午的光照进房间"。绝不写情绪、主观解读或谁的心情。
    只会投给此刻在 room_id 这个房间的角色（不在场的够不着）。

    Args:
        room_id: 这条动静发生 / 锚定在哪个房间。
        summary: 客观可感形态的文字描述。

    Returns:
        一句确认文本，含投给了谁。
    """
    lane, round_id = _world_round()

    # 本轮 emit 条数安全阀（决策 4）：正常够不着；触顶 logger.warning 不静默 +
    # 收口拒投，并把提示喂回模型让它停（不静默继续投）。这是"别失控空转"的机制
    # 边界，不替模型决策"产什么"（赤尾宪法）。
    counter = _emit_count()
    if counter["n"] >= WORLD_EMIT_SOFT_CAP:
        logger.warning(
            "world emit 触及本轮 soft cap %d（lane=%s round=%s），收口拒投后续 emit",
            WORLD_EMIT_SOFT_CAP,
            lane,
            round_id,
        )
        WORLD_EMIT_EVENTS.labels(status="capped").inc()
        return (
            f"这一轮产出的动静已达上限（{WORLD_EMIT_SOFT_CAP} 条），不再继续 emit。"
            f"请停止产出动静、用 sleep 定下次再看一眼世界。"
        )
    counter["n"] += 1

    recipients = await personas_in_room(lane=lane, room_id=room_id)
    event_id = derive_event_id(
        lane=lane, room_id=room_id, summary=summary, round_id=round_id
    )
    # occurred_at 跟现实走，由代码填（客观时间不让模型编）。
    from datetime import datetime, timedelta, timezone

    occurred_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    # 逐个独立投递：一个 recipient 失败不影响其他人，失败的 persona log 出来
    # （建议项 4）。整条 emit 不因单人失败被 @tool_error 包成错误炸掉。
    succeeded: list[str] = []
    failed: list[str] = []
    for persona_id in recipients:
        try:
            await deliver_event(
                lane=lane,
                persona_id=persona_id,
                event_id=event_id,
                summary=summary,
                occurred_at=occurred_at,
                kind=EVENT_KIND_AMBIENT,
                source="world",
                room_id=room_id,
            )
            succeeded.append(persona_id)
        except Exception:
            failed.append(persona_id)
            logger.warning(
                "world emit 投递给 %s 失败（lane=%s room=%s event=%s），其余收件人不受影响",
                persona_id,
                lane,
                room_id,
                event_id,
                exc_info=True,
            )
    WORLD_EMIT_EVENTS.labels(
        status="delivered" if recipients else "empty_room"
    ).inc()
    if not recipients:
        return f"{room_id} 此刻没人在，这条动静没人感知到"
    if not succeeded:
        return f"在 {room_id} 产生动静，但投递都失败了（{', '.join(failed)}）"
    msg = f"已在 {room_id} 产生动静，投给了 {', '.join(succeeded)}"
    if failed:
        msg += f"（{', '.join(failed)} 投递失败）"
    return msg


@tool
@tool_error("安排下次醒来失败")
async def sleep(
    seconds: Annotated[int, Field(description="多少秒后再看一眼世界，必须 ≤ 3600")],
) -> str:
    """决定过多久再看一眼世界（你唯一的自排手段）。

    看完这一轮，用它定下次多久再醒来看世界。``seconds`` 必须 ≤ 3600（最长睡 1h）。
    超过 1h 会报错，请改填一个 ≤ 3600 的值重调。平淡时段也别睡太久——世界是持续
    活的。

    Args:
        seconds: 多少秒后再看一眼世界（≤ 3600）。

    Returns:
        一句确认文本。
    """
    if seconds > WORLD_SLEEP_MAX_SECONDS:
        WORLD_SLEEP_REJECTED.inc()
        raise ValueError(
            f"sleep 的 seconds={seconds} 超过上限 {WORLD_SLEEP_MAX_SECONDS} 秒"
            f"（最长睡 1h）。请改填一个 ≤ {WORLD_SLEEP_MAX_SECONDS} 的值重调。"
        )
    if seconds < 0:
        seconds = 0
    # 不直接 emit_delayed：那样一轮内多次 sleep / 多轮 sleep 会各排一条未来 self
    # WorldTick → 叠加 heartbeat 唤醒风暴（决策 4 命门）。改为只把待办 self-wake
    # 记进 round-scoped state（覆盖而非追加 → 一轮内最后一次 sleep 为准），由
    # engine 在循环收口后 emit 至多一条 self WorldTick。
    _self_wake_slot()["delay_ms"] = seconds * 1000
    return f"好，{seconds} 秒后再看一眼世界"


async def fire_self_wake(*, lane: str, self_wake: dict) -> bool:
    """循环收口后 emit 至多一条 self ``WorldTick``（决策 4 唤醒风暴命门）。

    engine 在 agent 循环跑完后调本函数。``self_wake`` 是本轮 round-scoped 的待办
    self-wake 容器（:func:`sleep` 写的 ``{"delay_ms": int}``，覆盖而非追加 → 一轮
    内多次 sleep 最后一次为准）。有待办就 emit 唯一一条 self WorldTick；没调过
    sleep（空容器）就不 emit，靠 10min 保底心跳兜底。返回是否 emit。

    self-wake 的实际投递（``emit_delayed``）留在本模块：world 的自排是工具域的
    事，engine 只在循环收口处触发，firing 机制单一收口在这里。
    """
    delay_ms = (self_wake or {}).get("delay_ms")
    if delay_ms is None:
        return False
    # 延迟唤醒在 engine 里 import，避免 tools ↔ engine 循环 import。
    from app.world.engine import WorldTick

    await emit_delayed(WorldTick(lane=lane, reason="self"), delay_ms=delay_ms)
    return True


WORLD_TOOLS = [move_persona, emit_event, sleep]
