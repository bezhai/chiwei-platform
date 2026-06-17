"""世界存活探针 —— 只告警、绝不叫人、独立于世界心跳（spec Task 3 + 决策 7）.

兜住唯一没被业务逻辑覆盖的单点：world 自己的进程或心跳源挂了、长时间没按自己的
节奏醒来，全员无声睡死还查不出来。探针周期跑一次、读 world 最新一条客观世界状态
（``WorldState``）声明的下次醒时刻判它有没有按自己的节奏醒来，异常就发一条飞书
告警给人。

它和被否的 fan-out 定时心跳本质不同（决策 7）：fan-out 心跳是代码定时扫角色、替
世界叫人（替 agent 决策）；本探针只盯「世界多久没醒了」、超时告警给人，**绝不**碰
世界内容、**绝不**触发任何 notify / deliver_event / 角色唤醒。

判据（codex 必改、最关键）——绝不用「world_state 多久没更新 + 固定阈值」
--------------------------------------------------------------------------------
world 的保底心跳没到它自己排的 ``next_wake_at`` 时会 gate out、那一轮**不写**
world_state；而 world 正常 sleep 合法可达 1h。所以合法长睡也会让 world_state 长时间
不更新，固定阈值（如 600s 心跳 + 容错）会把合法长睡误报成死。正确判据是用 world
自己最新一条 world_state 声明的 ``next_wake_at`` 做基准：

  * ``next_wake_at`` 在：过了它承诺的下次醒时刻 + 容错、却还没有更新的 world_state，
    才算它没按自己的节奏醒来、判异常告警。还没到（合法长睡中）不告警。
  * ``next_wake_at`` 为 None（从没排过下次醒：首轮 / 只 update_world 没 sleep）：
    fallback 到「world 上次写状态时刻 + 最长合法 sleep(1h) + 容错」仍没更新才告警。
  * 该 lane 从没有任何 world_state（从没启动过）：不告警——那不是睡死、是没起过。

独立性（决策 7 关键约束）
--------------------------------------------------------------------------------
探针**必须独立于世界心跳**：窄入口 ``python -m app.monitoring.world_liveness_probe``
只连库、读、判、告警，**不 import app.wiring、不走 prepare_for_run / Runtime、不启动
任何 source loop**。否则又把 world 心跳拉进同进程，world 心跳挂了探针跟着挂、兜不住。

fail-open 铁律
--------------------------------------------------------------------------------
探针是带外观测，自身任何一步（读库 / 拼文本 / POST webhook）失败都只 log、绝不向上
抛、绝不崩——它崩了顶多丢一次告警，不能反过来变成新的故障源。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import text

from app.data.session import get_session
from app.infra import cst_time
from app.runtime.data import key_fields, version_field
from app.runtime.migrator import _table_name
from app.world.state import WorldState

logger = logging.getLogger(__name__)

# 告警 webhook 的独立 env（独立 URL、独立群，与 persona diff 告警分开避免混淆）。
# 值是完整的飞书 incoming webhook 地址（PaaS App envs 注入，token 敏感不进 git）。
WORLD_VITALITY_WEBHOOK_URL_ENV = "WORLD_VITALITY_WEBHOOK_URL"

_WEBHOOK_TIMEOUT_SECONDS = 10.0

# 最长合法 sleep：world 自排上限 1h（对齐 app.world.tools.WORLD_SLEEP_MAX_SECONDS）。
# next_wake_at 为 None 时的 fallback 基准——相对 world 上次写状态时刻量这么久没更新
# 才算异常。不 import world.tools 那个常量以免把 world 模块的副作用拉进探针进程；
# 这里独立钉一份、语义对齐即可（1h 是 world 自排上限的产品契约、不是实现细节）。
MAX_LEGAL_SLEEP = timedelta(hours=1)

# 容错：world 醒来本就接受几分钟、最坏接近一个 sleep 周期的漂移（spec non-goal）。
# 探针在 world 承诺 / fallback 的醒来时刻之上再宽这么久，过了才判异常，避免把正常
# 漂移误报。默认 10 分钟（= 一个保底心跳周期），运维侧可按需调窄 / 调宽。
DEFAULT_TOLERANCE = timedelta(minutes=10)


@dataclass(frozen=True)
class WorldLivenessBasis:
    """判据所需的最小基准：world 声明的下次醒时刻 + 它上次写状态的现实时刻。

    ``next_wake_at`` 是 world 自己在最新一版 world_state 里声明的下次该醒时刻
    （现实 aware ISO，可能为 None：从没排过）。``last_wrote_at`` 是该行 framework
    自动写的 ``created_at``（world 上次落状态的真实时刻），供 None fallback 量。
    """

    next_wake_at: str | None
    last_wrote_at: datetime


@dataclass(frozen=True)
class LivenessVerdict:
    """判据结论：是否告警 + 用到的截止时刻（供告警文案与日志说清缘由）。"""

    should_alert: bool
    deadline: datetime
    basis_reason: str  # "next_wake_at" | "fallback_max_sleep"


def judge_world_liveness(
    *,
    next_wake_at: str | None,
    last_wrote_at: datetime,
    now: datetime,
    tolerance: timedelta,
    max_legal_sleep: timedelta,
) -> LivenessVerdict:
    """核心判据（纯函数）：world 有没有按自己声明的节奏醒来。

    优先用 world 自己声明的 ``next_wake_at`` 做基准（截止 = next_wake_at + 容错）；
    它为 None 或脏到解析不出时退回 fallback（截止 = 上次写状态时刻 + 最长合法
    sleep + 容错）。``now`` 超过截止 = world 没按自己的节奏醒来、判告警。

    时刻解析与比较一律走 :mod:`app.infra.cst_time`（与 world 到点 gate 同口径）。
    """
    target = cst_time.parse(next_wake_at) if next_wake_at else None
    if target is not None:
        deadline = target + tolerance
        reason = "next_wake_at"
    else:
        # None（从没排过）或脏（解析不出，不该发生）：退回相对上次写状态的 fallback。
        deadline = last_wrote_at + max_legal_sleep + tolerance
        reason = "fallback_max_sleep"
    return LivenessVerdict(
        should_alert=now > deadline,
        deadline=deadline,
        basis_reason=reason,
    )


async def _read_latest_world_basis(*, lane: str) -> WorldLivenessBasis | None:
    """读某 lane 最新一版 world_state 的 ``next_wake_at`` + 框架 ``created_at``。

    **不复用** :func:`app.world.state.read_world_state`：它走 ``select_latest`` 只
    重建声明字段、会丢掉框架管理的 ``created_at``（world 上次写状态时刻），而 None
    fallback 判据要的就是它。所以这里直接读最新一行的这两列（同 ``select_latest``
    的 ``DISTINCT ON (key) ... ORDER BY version DESC`` 取最新一版语义）。

    没有任何 world_state 行（该 lane 从没启动过）返回 None——上层据此判「不是睡死、
    是没起过」、不告警。
    """
    table = _table_name(WorldState)
    keys = key_fields(WorldState)
    ver = version_field(WorldState)
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    order = f"{', '.join(keys)}, {ver} DESC" if ver else ", ".join(keys)
    sql = (
        f"SELECT DISTINCT ON ({', '.join(keys)}) next_wake_at, created_at "
        f"FROM {table} WHERE {where} ORDER BY {order}"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), {"lane": lane})
        row = r.mappings().first()
    if not row:
        return None
    return WorldLivenessBasis(
        next_wake_at=row["next_wake_at"],
        last_wrote_at=row["created_at"],
    )


def _render_alert_text(*, lane: str, now: datetime, verdict: LivenessVerdict) -> str:
    """告警文本：哪个 lane、现在几点、按什么基准判的、该醒的截止时刻是几点。"""
    return (
        f"【世界存活告警】lane={lane} 的世界可能停摆了。\n"
        f"现实此刻 {now.isoformat()}，已过它承诺的醒来截止 "
        f"{verdict.deadline.isoformat()}（判据：{verdict.basis_reason}）仍没有更新的"
        f"世界状态——world 进程或心跳源可能挂了，请尽快排查。"
    )


async def send_world_vitality_alert(*, lane: str, text: str) -> None:
    """把一条世界存活告警 POST 到独立的飞书 webhook。**本函数绝不向上抛**。

    env ``WORLD_VITALITY_WEBHOOK_URL`` 缺省 / 空白 = 不推（info 留痕）；POST 失败
    （HTTP 非 2xx / 飞书 body code != 0 / 连接异常）= error 留痕后吞掉——探针是带外
    观测，告警发不出去顶多丢一次告警，不能反过来变成新故障源。
    """
    try:
        url = (os.getenv(WORLD_VITALITY_WEBHOOK_URL_ENV) or "").strip()
        if not url:
            logger.info(
                "[world_vitality] %s env %s 未配置，存活告警只 log 不推送",
                lane,
                WORLD_VITALITY_WEBHOOK_URL_ENV,
            )
            return
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json={"msg_type": "text", "content": {"text": text}},
            )
        if not resp.is_success:
            logger.error(
                "[world_vitality] %s webhook 返回 HTTP %d（fail-open，不向上抛）：%s",
                lane,
                resp.status_code,
                resp.text[:200],
            )
            return
        body = resp.json()
        if body.get("code", 0) != 0:
            logger.error(
                "[world_vitality] %s 飞书返回错误码 %s（fail-open，不向上抛）：%s",
                lane,
                body.get("code"),
                body.get("msg"),
            )
            return
        logger.info("[world_vitality] %s 存活告警已推飞书 webhook", lane)
    except Exception:
        logger.error(
            "[world_vitality] %s 存活告警推送失败（fail-open，不向上抛）",
            lane,
            exc_info=True,
        )


async def run_probe(
    *,
    lane: str,
    now: datetime,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> None:
    """一次存活检查：读最新世界状态 → 判存活 → 异常才告警。**绝不向上抛**。

    fail-open：读库 / 判据 / 告警任何一步炸都只 error 留痕、不向上抛、不崩。探针
    全程只读 + 只告警，**不**调用任何唤醒角色 / 跑世界推演 / 启动 runtime 的东西。
    """
    try:
        basis = await _read_latest_world_basis(lane=lane)
        if basis is None:
            logger.info(
                "[world_vitality] %s 还没有任何世界状态（从没启动过），不告警", lane
            )
            return
        verdict = judge_world_liveness(
            next_wake_at=basis.next_wake_at,
            last_wrote_at=basis.last_wrote_at,
            now=now,
            tolerance=tolerance,
            max_legal_sleep=MAX_LEGAL_SLEEP,
        )
        if not verdict.should_alert:
            logger.info(
                "[world_vitality] %s 世界存活正常（基准=%s，截止=%s，现在=%s）",
                lane,
                verdict.basis_reason,
                verdict.deadline.isoformat(),
                now.isoformat(),
            )
            return
        logger.error(
            "[world_vitality] %s 世界可能停摆：现在=%s 已过截止=%s（基准=%s），发告警",
            lane,
            now.isoformat(),
            verdict.deadline.isoformat(),
            verdict.basis_reason,
        )
        await send_world_vitality_alert(
            lane=lane,
            text=_render_alert_text(lane=lane, now=now, verdict=verdict),
        )
    except Exception:
        logger.error(
            "[world_vitality] %s 探针自身异常（fail-open，不向上抛）",
            lane,
            exc_info=True,
        )


def _main() -> None:
    """窄入口：``python -m app.monitoring.world_liveness_probe``。

    刻意只做四件事——解析 lane、连库读、判、告警——**绝不** import app.wiring、
    **绝不** 走 prepare_for_run / Runtime、**绝不**启动任何 source loop。否则又把
    world 心跳的那套调度拉进本进程，失去「独立于世界心跳」的存在意义（决策 7）。
    """
    import asyncio

    from app.runtime.lane_policy import current_deployment_lane

    lane = current_deployment_lane() or "prod"
    now = cst_time.now_cst()
    asyncio.run(run_probe(lane=lane, now=now))


if __name__ == "__main__":
    _main()
