"""客观世界叙述快照 — 阶段 1A（world 推演者）.

world 只剩一块客观状态：``WorldState`` —— 这家的客观世界叙述快照（``world_time``
+ 一段自然语言 ``detail`` 叙述），as_latest（append-only + 读最新一版）、Key 带
lane（泳道隔离命门：coe / ppe 绝不能覆盖 prod 的"她此刻客观世界"）。

新范式下没有 presence 表了。旧设计里 world 是导演——拿 ``RoomPresence`` 查表记
"谁在哪个房间"、按同-room 机械匹配投递。现在 world 是推演者：世界此刻什么样、
谁大概在哪在干嘛，全融进 ``detail`` 自然语言叙述里，由 world 推演维护；谁够得着
一条动静也由 world 推演（不查表）。所以这里只剩世界叙述一块快照，没有任何结构化
在场表。

写入走 framework 的 ``insert_append``（Version 自增），读走 ``select_latest``
（每个 key 取最新一版）——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class WorldState(Data):
    """这家的客观世界叙述快照：世界时间 + 一段自然语言叙述。

    自然键 ``lane``（每个泳道一份客观世界）。``world_time`` / ``detail`` 都是
    TEXT —— 客观世界叙述就是一段自然语言、不拆结构化表，是 world 的形态选择
    （framework persist 层已支持 dict/list → JSONB，这里只放可读文本是设计、不是限制）。
    ``detail`` 是 world 推演出来的"世界此刻什么样"（谁大概在哪、在干嘛、什么氛围，
    位置融在叙述里），``version`` 让同一 lane 的多版快照 append-only 保留历史、读
    最新一版。
    """

    lane: Annotated[str, Key]
    world_time: str  # 世界此刻 (ISO8601)
    detail: str      # 世界此刻的客观叙述（world 推演出来的一段自然语言）
    # 下次该醒的**现实**时刻（CST aware ISO，阶段 1B 到点 gate）。world 调 sleep 后
    # 由收口 fire_self_wake 写进来；world 唤醒入口对 self / 心跳走 gate 时读它判到点。
    # nullable：从没排过下次醒（首轮 / 只 update_world 没 sleep）时为 None，心跳放行
    # 别卡死。framework migrate 对已有数据的表加 nullable 列是 additive、不阻塞。比较
    # 一律用现实时间、不用 world_time（world_time 会因 gate 停滞）。
    next_wake_at: str | None = None
    # act 消费游标（pull 范式）：上次推完一批 act 推进到的复合游标
    # ``(created_at, act_id)``。world 醒来读"游标之后的 act"，推完把游标推进到本批
    # 末尾。游标用 ``created_at``（framework 给每行自动加的单调落库时刻）而非
    # ``occurred_at``（life 轮首固定的做事时刻、与落库顺序可乱序）——按 occurred_at
    # 推进会先消费"晚发生但早落库"的 act 把游标推过去、之后落库的"早发生" act 永远
    # 读不到（漏 act）；created_at 单调落库序、按它推进不漏（命门见 acts.py docstring）。
    # 复合游标是因为同一落库瞬间理论上可能多条 act：只用 created_at 的 ``>`` 漏边界同刻
    # 新行、``>=`` 重读边界旧行；加 act_id 作稳定 tie-breaker 两头都不错。nullable：从没
    # 消费过（冷启动）时为 None，读全既有 act。两列同样是 additive nullable migrate。
    act_cursor_created_at: str | None = None
    act_cursor_act_id: str | None = None
    # 已纳入「今天的外部底料」的那一天（CST `YYYY-MM-DD`）。world 当天第一次醒、有
    # 底料且这个字段 != 今天时，把底料（DailyMaterials.briefing）当公共背景纳入一次、
    # 进意识流 transcript；纳入那轮收口把这个字段标成今天，当天后续轮次（== 今天）不
    # 再重喂；跨到第二天有新底料（!= 今天）再重新纳入一次。nullable：从没纳入过（首轮
    # / 冷启）时为 None，与"任何今天"都不相等、首醒就纳入。同样是 additive nullable
    # migrate（对齐 next_wake_at / act 游标）。**默认 None 不撞 migrator 保留列**
    # （id / created_at / updated_at / dedup_hash）。
    materials_ingested_date: str | None = None
    # 「当日反思（对表翻页）已完成」的那一天（CST `YYYY-MM-DD`，Task 2b）。独立于
    # materials_ingested_date（续写成功不代表反思成功——spec 决策 5）：engine 在该
    # 字段 != 今天（含 None=冷启 / 部署后首跑）时、续写之前跑一次无会话反思；反思
    # **成功**才由 mark_arc_reflected 落成今天，失败不落 → 同日后续轮自动重试。
    # nullable：从没反思过时为 None，与"任何今天"都不相等、首轮就反思。additive
    # nullable migrate（对齐 materials_ingested_date），默认 None 不撞 migrator
    # 保留列（id / created_at / updated_at / dedup_hash）。
    arc_reflected_date: str | None = None
    # 「当日底料已被反思消化」的那一天（CST `YYYY-MM-DD`，眼睛闭环的第二班标记）。
    # world 24×7，每天 00:0X 首轮就触发第一班反思（arc_reflected_date）——那时眼睛
    # 还没出门、当天底料不存在，单一标记会让「当天 briefing 永远不被当天反思消化」。
    # 所以 engine 在「当日底料存在且本字段 != 今天」时补一班带底料的反思；带底料的
    # 成功反思同时落两个标记（它已覆盖两班职责，避免冗余第二班），无底料的成功反思
    # 只落 arc_reflected_date、不碰本字段。同样成功才落、失败同日重试。nullable +
    # additive migrate（对齐 arc_reflected_date），默认 None 不撞 migrator 保留列。
    arc_materials_reflected_date: str | None = None
    # 已纳入「世界的固定人物」名册的那一天（CST `YYYY-MM-DD`，NPC 层第一刀）。world
    # 当天第一次醒、名册非空且这个字段 != 今天时，把整份 NPCRoster 名册（按所属姐妹
    # 归类的速写）当公共背景纳入一次、进意识流 transcript；纳入那轮收口把这个字段标成
    # 今天，当天后续轮次（== 今天）不再重喂；跨到第二天（!= 今天）再重新纳入一次。
    # 与 materials_ingested_date **独立**（名册 seed 后总在、底料某天可能没有——两件
    # 不相干的事，各用各的游标）。nullable：从没纳入过（首轮 / 冷启）时为 None，与
    # "任何今天"都不相等、首醒就纳入。additive nullable migrate（对齐
    # materials_ingested_date），默认 None 不撞 migrator 保留列。
    roster_ingested_date: str | None = None
    version: Annotated[int, Version] = 0


async def write_world_state(*, lane: str, world_time: str, detail: str) -> None:
    """append 一版客观世界叙述快照（world 用 update_world 工具推演完落最新世界状态）。

    update_world 工具在循环中途调，只更新世界叙述（world_time + detail），**保留**
    收口才写的调度状态（``next_wake_at`` / act 游标）。否则每轮更新叙述都把游标 /
    到点时刻打回 None，下轮重读全部 act / 长睡意愿失效。WorldState 是 append-only：
    读最新一版、沿用它的调度状态，只换 world_time / detail，append 一版。冷启动
    （首版还没快照）时无可沿用的调度状态，照常以 None 起。
    """
    prev = await read_world_state(lane=lane)
    await insert_append(
        WorldState(
            lane=lane,
            world_time=world_time,
            detail=detail,
            next_wake_at=prev.next_wake_at if prev is not None else None,
            act_cursor_created_at=prev.act_cursor_created_at if prev is not None else None,
            act_cursor_act_id=prev.act_cursor_act_id if prev is not None else None,
            materials_ingested_date=(
                prev.materials_ingested_date if prev is not None else None
            ),
            arc_reflected_date=(
                prev.arc_reflected_date if prev is not None else None
            ),
            arc_materials_reflected_date=(
                prev.arc_materials_reflected_date if prev is not None else None
            ),
            roster_ingested_date=(
                prev.roster_ingested_date if prev is not None else None
            ),
        )
    )


async def read_world_state(*, lane: str) -> WorldState | None:
    """读某泳道客观世界最新一版叙述快照，没有返回 None（冷启动）。"""
    return await select_latest(WorldState, {"lane": lane})


async def set_next_wake_at(*, lane: str, next_wake_at: str) -> None:
    """记下 world 下次该醒的现实时刻（阶段 1B 到点 gate）。

    world 调 sleep 决定下次几时醒后，由收口 :func:`app.world.tools.fire_self_wake`
    把目标唤醒时刻（现实 now + sleep 秒数）写进来。WorldState 是 append-only：这里
    读最新一版、沿用它的 ``world_time`` / ``detail`` / act 游标（不丢世界叙述、不丢
    已消费游标），只把 ``next_wake_at`` 换成新目标，append 一版。

    冷启容错：还没有任何 WorldState 快照（首轮还没 update_world 落叙述）时无可承载
    next_wake_at 的快照，安全跳过（不造空 detail 占位，世界叙述统一由 update_world
    工具落）。这种情形下 next_wake_at 没排上，靠保底心跳兜底——不抛、不卡死。
    """
    snapshot = await read_world_state(lane=lane)
    if snapshot is None:
        return
    await insert_append(
        WorldState(
            lane=lane,
            world_time=snapshot.world_time,
            detail=snapshot.detail,
            next_wake_at=next_wake_at,
            act_cursor_created_at=snapshot.act_cursor_created_at,
            act_cursor_act_id=snapshot.act_cursor_act_id,
            materials_ingested_date=snapshot.materials_ingested_date,
            arc_reflected_date=snapshot.arc_reflected_date,
            arc_materials_reflected_date=snapshot.arc_materials_reflected_date,
            roster_ingested_date=snapshot.roster_ingested_date,
        )
    )


async def advance_act_cursor(*, lane: str, created_at: str, act_id: str) -> None:
    """把 act 消费游标推进到本批末尾（pull 范式收口）。

    world 推完一批 act 成功收口后调本函数，把复合游标 ``(created_at, act_id)`` 推到
    本批最后一条 act 的坐标——下轮只读它之后的 act，不重读这批。``created_at`` 是该行
    framework 自动写的单调落库时刻（不是 occurred_at），按它推进不漏（命门见 acts.py）。
    WorldState 是 append-only：读最新一版、沿用它的 ``world_time`` / ``detail`` /
    ``next_wake_at``（不丢世界叙述、不丢到点时刻），只换 act 游标，append 一版。

    冷启容错：还没有任何 WorldState 快照（首版还没 update_world 落叙述）时无可承载
    游标的快照，安全跳过——这种情形下本就没消费到任何 act（冷启动批次推演里 world
    会先 update_world 落第一版），下次再推进。不抛、不卡死。
    """
    snapshot = await read_world_state(lane=lane)
    if snapshot is None:
        return
    await insert_append(
        WorldState(
            lane=lane,
            world_time=snapshot.world_time,
            detail=snapshot.detail,
            next_wake_at=snapshot.next_wake_at,
            act_cursor_created_at=created_at,
            act_cursor_act_id=act_id,
            materials_ingested_date=snapshot.materials_ingested_date,
            arc_reflected_date=snapshot.arc_reflected_date,
            arc_materials_reflected_date=snapshot.arc_materials_reflected_date,
            roster_ingested_date=snapshot.roster_ingested_date,
        )
    )


async def record_world_round_close(
    *,
    lane: str,
    advance_cursor_to: tuple[str, str] | None,
    materials_ingested_date: str | None,
    roster_ingested_date: str | None = None,
) -> None:
    """world 一轮成功推演收口：在**同一次** WorldState append 里推进 act 游标 + 标记
    底料已纳入今天 + 标记名册已纳入今天。

    一轮收口本有几件互不相干的调度状态要落：act 消费游标推进、「今天的外部底料」是否
    在这轮被纳入、「世界的固定人物」名册是否在这轮被纳入（纳入了就把对应日期标成今天，
    当天后续轮不再重喂）。它们都改 WorldState，若各写一版会多出冗余快照、还可能彼此
    覆盖（后写的读到前写之前的快照、把对方的改动丢掉）。所以并进一次 append：读最新一版，
    按入参选择性地改这几块、其余字段（world_time / detail / next_wake_at / 没改的标记）
    原样沿用，append 一版，原子、幂等。

      * ``advance_cursor_to``：``(created_at, act_id)`` —— 本批末尾游标。非空批传它、把
        游标推进过去；**空批次传 None** —— 没读到 act 没什么可推进，游标沿用上一版。
      * ``materials_ingested_date``：今天的日期串 —— 这轮纳入了底料就传它，标成今天；
        **没纳入（已纳入过 / 今天没底料）传 None** —— 不改，沿用上一版已有标记（绝不把
        已纳入标记清回 None，否则当天会反复重喂）。
      * ``roster_ingested_date``：今天的日期串 —— 这轮纳入了 NPC 名册就传它，标成今天；
        **没纳入（已纳入过 / 名册为空）传 None** —— 不改，沿用上一版（同 materials 的
        语义；名册与底料各用各的游标、互不打架）。

    崩溃语义命门：本函数只在 ``Agent.run`` 成功返回后（engine 收口）才调。run 中途抛
    时它根本不会被调到，游标不推进、materials_ingested_date / roster_ingested_date 不被
    误标记——下轮重醒重读这批 act、重新纳入底料 + 名册（act / 底料 / 名册都不丢）。

    冷启容错（对齐 :func:`advance_act_cursor`）：还没有任何 WorldState 快照时无可承载
    收口状态的快照，安全跳过——这种情形本就没消费到 act、世界叙述统一由 update_world
    工具落。不抛、不卡死。
    """
    snapshot = await read_world_state(lane=lane)
    if snapshot is None:
        return
    if advance_cursor_to is not None:
        cursor_created_at, cursor_act_id = advance_cursor_to
    else:
        cursor_created_at = snapshot.act_cursor_created_at
        cursor_act_id = snapshot.act_cursor_act_id
    await insert_append(
        WorldState(
            lane=lane,
            world_time=snapshot.world_time,
            detail=snapshot.detail,
            next_wake_at=snapshot.next_wake_at,
            act_cursor_created_at=cursor_created_at,
            act_cursor_act_id=cursor_act_id,
            materials_ingested_date=(
                materials_ingested_date
                if materials_ingested_date is not None
                else snapshot.materials_ingested_date
            ),
            arc_reflected_date=snapshot.arc_reflected_date,
            arc_materials_reflected_date=snapshot.arc_materials_reflected_date,
            roster_ingested_date=(
                roster_ingested_date
                if roster_ingested_date is not None
                else snapshot.roster_ingested_date
            ),
        )
    )


async def mark_arc_reflected(
    *, lane: str, date: str, materials_date: str | None = None
) -> None:
    """反思环节成功后把「当日反思已完成」标记落成 ``date``（CST ``YYYY-MM-DD``）。

    挂载条件的另一半在 engine：``arc_reflected_date != 今天`` 才跑第一班反思
    （Task 2b）；当日底料存在且 ``arc_materials_reflected_date != 今天`` 时补第二班
    （眼睛闭环）。本函数只在反思 Agent 调用**成功**后被调——失败不落标记、同日后续
    轮自动重试，直到成功才落（spec 决策 5：一天的机会不被吞掉）。

    ``materials_date``：这次反思**带底料**跑成时传当天日期——一次带底料的成功反思
    已覆盖两班职责，``arc_materials_reflected_date`` 同时落掉（避免冗余第二班）；
    **无底料**的反思传 None——不碰第二班标记、沿用上一版（绝不清回 None，否则白天
    底料落地后的补班会被误吞）。

    WorldState 是 append-only：读最新一版、沿用其余全部字段（叙述 / 到点时刻 /
    游标 / 底料标记都不丢），只换反思标记，append 一版。

    冷启动**不能**像其他调度写入点那样安全跳过：反思先于续写跑，冷启动反思成功
    落标时还没有任何 WorldState 行——跳过 = 标记丢失，同日每一轮都重跑反思（违反
    「每班同日至多一次、成功后同日不再重复跑」；与 set_next_wake_at /
    advance_act_cursor 的"跳过无害"不同，那两处丢的只是一次调度、有兜底）。所以
    冷启动插一行**最小占位快照**承载标记（带底料时两个标记一起承载）：叙述字段
    中性空白（``detail`` / ``world_time`` 空串，真实首版叙述仍由续写的 update_world
    写——:func:`write_world_state` 的保留链会把这里落的标记带上）、调度字段全 None
    （gate 对 None ``next_wake_at`` 的判定、游标为 None 读全量的行为都与真冷启
    一致）。engine 把「detail 空白」继续当冷启动分支对待（占位行不冒充世界叙述）。
    """
    snapshot = await read_world_state(lane=lane)
    if snapshot is None:
        await insert_append(
            WorldState(
                lane=lane,
                world_time="",
                detail="",
                arc_reflected_date=date,
                arc_materials_reflected_date=materials_date,
            )
        )
        return
    await insert_append(
        WorldState(
            lane=lane,
            world_time=snapshot.world_time,
            detail=snapshot.detail,
            next_wake_at=snapshot.next_wake_at,
            act_cursor_created_at=snapshot.act_cursor_created_at,
            act_cursor_act_id=snapshot.act_cursor_act_id,
            materials_ingested_date=snapshot.materials_ingested_date,
            arc_reflected_date=date,
            arc_materials_reflected_date=(
                materials_date
                if materials_date is not None
                else snapshot.arc_materials_reflected_date
            ),
            roster_ingested_date=snapshot.roster_ingested_date,
        )
    )
