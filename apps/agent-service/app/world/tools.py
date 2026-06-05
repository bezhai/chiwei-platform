"""world 的 agent 工具集 — 阶段 1A（world 推演者）.

world 不再"填一张表"返回结构化对象，而是在一个 agent 循环里**连续调工具去推演
世界**。新范式下 world 是世界的推演层、不是导演，它有三个工具：

  * :func:`update_world` —— 写一段自然语言、记"世界此刻什么样"。落 durable 快照。
    ``world_time`` 由工具体自填（现实当前 CST，客观时间不让模型编），``detail``
    是模型给的世界叙述，一起 ``write_world_state`` append 一版。
  * :func:`notify`       —— world 推演出"这条客观动静此刻谁够得着"，把 observation
    （自然语言客观描述）投给 recipients（persona_id 列表）。对每个 recipient 包
    ``deliver_event`` 投进其信箱（kind=ambient、source="world"、无房间锚点）。谁
    够得着由 world 在 prompt 层推演，工具只忠实把它推演出的 recipients 落投递。
  * :func:`sleep`        —— 决定下次多久再看一眼世界。它不直接 ``emit_delayed``，
    而是把待办 self-wake 记进本轮 round-scoped state（一轮内多次 sleep 覆盖而非
    追加，最后一次为准），由 engine 在循环收口后 emit 至多一条 self ``WorldTick``
    （避免多次 sleep 叠加未来唤醒风暴）。这是 world 唯一的自排手段。

工具体从 ambient :class:`~app.agent.context.AgentContext` 读 world 本轮的
``lane`` 和 ``round_id``（塞在 ``features`` 里）——lane / round 是机制层的事，
不是世界内容决策，所以不放进工具签名让模型填。``round_id`` 是本轮唤醒的确定性
标识，:func:`notify` 用它把 event_id 派生成确定的（lane + observation + round_id），
整轮重放时同一条动静落同一 id、靠 ``deliver_event`` 幂等去重不重复。

赤尾设计宪法：
  * :func:`notify` 的 observation 必须是客观可感形态、绝不写情绪 / 主观解读（这条
    在 prompt 层钉、工具只忠实落模型给的文字）。
  * 谁够得着由 world 推演（在 prompt 层判断），工具不查表、不机械匹配房间。
  * :func:`sleep` 的 60s~1h 上下限是"别睡死 / 别排太密"的机制边界（决定何时醒），
    不是用阈值替"产什么"的决策。超限**返回错误喂回模型让它重调**，绝不静默夹。
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
from app.infra import cst_time
from app.runtime.emit import emit_delayed  # module-level so tests can monkeypatch
from app.world.state import write_world_state

logger = logging.getLogger(__name__)

# sleep 上限：world 最长睡 1h。超限报错喂回模型，绝不静默夹。这是"别睡死"的机制
# 边界，不进世界内容决策（赤尾宪法）。
WORLD_SLEEP_MAX_SECONDS = 3600

# sleep 下限：world 自排最短 1 分钟。低于下限报错喂回模型重调（跟上限超限处理对称、
# 不静默夹），让常规 / self 自排不排得太密。注意下限只挡 sleep 自排——act 立即唤醒
# 由 act→world 边上的 60s 合并闸挡。
WORLD_SLEEP_MIN_SECONDS = 60

# round-scoped 可变 state 的 features key（engine 每轮新建、工具体跨调用读写、
# engine 收口后读取）。待办 self-wake 让一轮内多次 sleep 覆盖而非累积（唤醒风暴
# 命门，最后一次为准）。
FEATURE_SELF_WAKE = "world_self_wake"  # {} | {"delay_ms": int} —— 本轮待办 self-wake

# event_id 派生命名空间：固定 UUID，让 uuid5 在同一 (lane, observation, round) 上
# 稳定可复现（整轮重放幂等命门）。
_EVENT_ID_NS = uuid.UUID("6f1c8b2a-5e7d-4c3a-9f0b-1d2e3a4b5c6d")

WORLD_NOTIFY_EVENTS = get_or_create_counter(
    "world_notify_events_total", "world notify 投递的 event 计数", ["status"]
)
WORLD_SLEEP_REJECTED = get_or_create_counter(
    "world_sleep_rejected_total", "world sleep 超出 60s~1h 上下限被拒次数", []
)


def derive_event_id(*, lane: str, observation: str, round_id: str) -> str:
    """确定性派生一条动静的 id：同 (lane, observation, round_id) 同 id。

    整轮重放时模型重复调 notify 同一条 observation，派生出同一 event_id，
    ``deliver_event`` 按 (lane, persona, event_id) 幂等去重，不会重复投递。
    同一 observation 投多个 recipient 共享这一 id（persona 不同自然键不同，
    不冲突）。不同 observation（不同的动静）派生不同 id。不含房间——新范式没有
    房间锚点，谁够得着由 world 推演。
    """
    return uuid.uuid5(_EVENT_ID_NS, f"{lane}\x1f{observation}\x1f{round_id}").hex


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


def _self_wake_slot() -> dict:
    """本轮待办 self-wake 容器 ``{} | {"delay_ms": int}``（engine 每轮新建）。

    sleep 往里写 delay_ms（覆盖而非追加 → 一轮内多次 sleep 最后一次为准），
    engine 在循环收口后读它 emit 至多一条 self WorldTick。
    """
    return get_context().features.setdefault(FEATURE_SELF_WAKE, {})


@tool
@tool_error("更新世界叙述失败")
async def update_world(detail: str) -> str:
    """写下世界此刻的客观叙述（记"世界此刻什么样"）。

    看一眼你记得的上一版世界叙述 + 现在几点，推演世界此刻什么样：谁大概在哪、
    在干嘛、什么氛围（位置融在叙述里）；对收到的角色动作，把它的客观结果也体现
    进来（她去厨房 → 厨房有了动静和她的身影）。detail 只写客观发生了什么，绝不
    写谁的情绪 / 心情 / 主观解读——那是角色自己的事。

    世界时间由系统按现实当前时刻自动记下（你不用、也不能编世界时间）。

    Args:
        detail: 世界此刻的客观叙述（一段自然语言）。

    Returns:
        一句确认文本。
    """
    lane, _round_id = _world_round()
    # world_time 跟现实走，由代码填现实当前 CST（客观时间不让模型编）。
    world_time = cst_time.now_cst_iso()
    await write_world_state(lane=lane, world_time=world_time, detail=detail)
    return "已记下世界此刻的样子"


@tool
@tool_error("投递动静失败")
async def notify(recipients: list[str], observation: str) -> str:
    """把一条客观动静投给你推演出"此刻够得着它"的角色。

    observation 必须是客观可感的形态（感官投影），比如"厨房飘来煎蛋和咖啡的香味"
    "玄关传来开关门的声音""晌午的光斜照进房间"。绝不写情绪、心情、主观解读、
    建议或指令。

    recipients 是你推演出此刻够得着这条动静的角色 id 列表（在厨房的人闻得到厨房
    的香味、在学校的够不着）——谁够得着由你判断，不是查表。没人够得着就传空列表。

    Args:
        recipients: 此刻够得着这条动静的角色 id 列表（如 ["chinagi", "ayana"]）。
        observation: 客观可感形态的文字描述。

    Returns:
        一句确认文本，含投给了谁。
    """
    lane, round_id = _world_round()

    if not recipients:
        WORLD_NOTIFY_EVENTS.labels(status="nobody").inc()
        return "这条动静此刻没人够得着，没投给任何人"

    event_id = derive_event_id(lane=lane, observation=observation, round_id=round_id)
    # occurred_at 跟现实走，由代码填（客观时间不让模型编）。
    occurred_at = cst_time.now_cst_iso()
    # 逐个独立投递：一个 recipient 失败不影响其他人，失败的 persona log 出来。
    # 整条 notify 不因单人失败被 @tool_error 包成错误炸掉。
    succeeded: list[str] = []
    failed: list[str] = []
    for persona_id in recipients:
        try:
            await deliver_event(
                lane=lane,
                persona_id=persona_id,
                event_id=event_id,
                summary=observation,
                occurred_at=occurred_at,
                kind=EVENT_KIND_AMBIENT,
                source="world",
            )
            succeeded.append(persona_id)
        except Exception:
            failed.append(persona_id)
            logger.warning(
                "world notify 投递给 %s 失败（lane=%s event=%s），其余收件人不受影响",
                persona_id,
                lane,
                event_id,
                exc_info=True,
            )
    WORLD_NOTIFY_EVENTS.labels(status="delivered").inc()
    if not succeeded:
        return f"这条动静投递都失败了（{', '.join(failed)}）"
    msg = f"已把这条动静投给 {', '.join(succeeded)}"
    if failed:
        msg += f"（{', '.join(failed)} 投递失败）"
    return msg


@tool
@tool_error("安排下次醒来失败")
async def sleep(
    seconds: Annotated[
        int, Field(description="多少秒后再看一眼世界，必须在 60～3600 之间")
    ],
) -> str:
    """决定过多久再看一眼世界（你唯一的自排手段）。

    看完这一轮，用它定下次多久再醒来看世界。``seconds`` 必须在 60～3600 之间
    （最短睡 1 分钟、最长睡 1h）。超出范围会报错，请改填一个 60～3600 的值重调。

    Args:
        seconds: 多少秒后再看一眼世界（60 ≤ seconds ≤ 3600）。

    Returns:
        一句确认文本。
    """
    if seconds > WORLD_SLEEP_MAX_SECONDS:
        WORLD_SLEEP_REJECTED.inc()
        raise ValueError(
            f"sleep 的 seconds={seconds} 超过上限 {WORLD_SLEEP_MAX_SECONDS} 秒"
            f"（最长睡 1h）。请改填一个 ≤ {WORLD_SLEEP_MAX_SECONDS} 的值重调。"
        )
    if seconds < WORLD_SLEEP_MIN_SECONDS:
        WORLD_SLEEP_REJECTED.inc()
        raise ValueError(
            f"sleep 的 seconds={seconds} 低于下限 {WORLD_SLEEP_MIN_SECONDS} 秒"
            f"（最短睡 1 分钟）。请改填一个 ≥ {WORLD_SLEEP_MIN_SECONDS} 的值重调。"
        )
    # 不直接 emit_delayed：那样一轮内多次 sleep / 多轮 sleep 会各排一条未来 self
    # WorldTick → 叠加 heartbeat 唤醒风暴。改为只把待办 self-wake 记进 round-scoped
    # state（覆盖而非追加 → 一轮内最后一次 sleep 为准），由 engine 在循环收口后
    # emit 至多一条 self WorldTick。
    _self_wake_slot()["delay_ms"] = seconds * 1000
    return f"好，{seconds} 秒后再看一眼世界"


async def fire_self_wake(*, lane: str, self_wake: dict) -> bool:
    """循环收口后 emit 至多一条 self ``WorldTick``（唤醒风暴命门）。

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


WORLD_TOOLS = [notify, update_world, sleep]
