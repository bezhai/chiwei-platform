"""NotebookEntry — 赤尾随身小本子的一条条目（备忘录 & 日程 app 的底子）.

「手机」概念的第一个 app。她在 life 里自己记下一件事：没挂时间的是备忘录（想做的
事、惦记的人、突然的念头），挂了时间的是日程（到点要做 / 被提醒）。**一个本子、
两种条目，差别只在 ``remind_at`` 挂没挂**——一条事哪天加个时间就从备忘录变成日程，
拆成两张表就得来回搬，所以不拆（spec「一个本子、两种条目」）。

设计上钉死的几条：

  * **不做优先级 / 标签 / 分类那套结构化**（spec：人记备忘不填表）。条目就是一句
    大白话 ``content`` + 可选 ``remind_at`` + 一个状态 ``status``。结构化字段是设计
    禁区、不是 framework 限制。
  * **三态 ``status``**：``active`` 还惦记 / ``done`` 做了 / ``dropped`` 划了。**没有
    「到点了」这种存储态**——「到点了」是翻本子时按「有 remind_at 且还 active 且时刻
    已过」**派生显示**的，不落库（落库就要定时器替她标，spec 明禁：状态全是她自己的
    判断、没有任何定时器）。
  * **as_latest + Version，Key 带 lane**：改 / 划一条 append 一版，对外读永远
    ``select_latest`` 取最新一版（旧版留作历史，不删；睡前清理也是 append 一版
    done/dropped，不是物理删）。Key 含 lane —— runtime 持久化不会自动加 lane，不显式
    带上 coe / ppe 泳道就会覆盖 prod 的本子（写脏线上她的私人内容），同其它 durable
    Data（LifeState / DayPage）。

``noted_at`` 而非 ``created_at``：``created_at`` 是 runtime 保留列（migrator 自动加的
落库时刻），不能拿来当业务字段名（同 LifeState.observed_at / DayPage.written_at 的
教训）。``noted_at`` 是她记下这条的现实时刻（按她输入里的「现在几点」算）。

「记一条」幂等（durable mutation，对称 act 的 ``perform_act``）：首写走
``insert_idempotent``（ver=0），整轮重试 / durable 重投用同一 ``(lane, persona_id,
entry_id)`` 再写一次 —— dedup_hash 折进 ver=0 → ON CONFLICT DO NOTHING、只落一条。
改 / 划走 ``insert_append`` append 新版（ver 自增）。
"""

from __future__ import annotations

from typing import Annotated

from app.infra import cst_time
from app.runtime.data import Data, Key, Version
from app.runtime.persist import (
    insert_append,
    insert_idempotent,
    select_latest,
)

# 三态协议常量（机制层硬定，不是让 LLM 猜的字符串）。翻本子 / 进她输入 / 睡前清理
# 都按这几个值判「还活着」（== active）。单一定义处（宪法「禁止重复定义」）。
STATUS_ACTIVE = "active"    # 还惦记（她没标 done / dropped 的）
STATUS_DONE = "done"        # 做了
STATUS_DROPPED = "dropped"  # 不做了，划掉

# 「她自己还没了结」的态集合：进她每轮输入 / 翻本子默认看的就是这些。**不是代码按
# 年龄 / 条数 / 过期去筛**（那就成了代码替她决定忘掉什么、违宪）——只看她自己有没有
# 标 done / dropped。
ACTIVE_STATUSES = frozenset({STATUS_ACTIVE})

# 全部合法 status（写入只允许这三个值）。拼错（如 complete 而非 done）的条目既不在
# active-only（不进她输入）、又被 reminder gate 当非 active 丢掉 → 静默失踪、她再也
# 看不到。写入处守住、非法 fail-fast，比写脏后静默消失好。单一定义处（宪法）。
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DONE, STATUS_DROPPED})


def _validate_status(status: str) -> None:
    """status 写入校验（机制护栏，不替她决策）：只允许三态，非法 fail-fast 抛 ValueError。

    工具层 @tool_error 把它喂回模型重填——挡的是无效工具参数（拼错的 status），不是
    替她判这条该是什么态。在领域层 :func:`update_entry` 写入前守住，脏 status 根本进
    不来（否则拼错会让条目静默失踪：既不进 active-only 输入、又被 reminder gate 丢掉）。
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"status={status!r} 不是合法状态（只能是 "
            f"{', '.join(sorted(VALID_STATUSES))}）。请改填一个合法状态重调。"
        )


def _validate_remind_at(remind_at: str) -> None:
    """remind_at 写入校验（机制护栏）：必须是合法 ISO 时刻，脏串 fail-fast 抛 ValueError。

    复用 :func:`app.infra.cst_time.parse`（现有 ISO / Unix 毫秒解析口径，单一来源）判
    能否解析出真实时刻。脏串（解析返回 None）→ 抛 ValueError 让模型重填。否则脏串落库
    后渲染侧当「还惦记 / 没到点」、调度侧 fire_schedule_reminders 解析不了夹成 delay=0
    → 立即错误提醒，两边不一致。写入前守住让脏串根本进不来、渲染 / 调度自然一致。
    """
    if cst_time.parse(remind_at) is None:
        raise ValueError(
            f"remind_at={remind_at!r} 不是合法的时刻（要 ISO8601，如 "
            "2026-06-13T15:00:00+08:00）。请改填一个合法时刻重调。"
        )


class NotebookEntry(Data):
    """她本子里的一条：一句话 + 可选提醒时间 + 状态。as_latest（带 Version）。

    自然键 ``(lane, persona_id, entry_id)``：泳道隔离 + 每人一个本子 + 每条一条版本
    链。``remind_at is None`` = 备忘录，有值 = 日程（到点提醒）——两类只差这一个字段。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    entry_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    content: str                 # 她用大白话写的一句话（这件事）
    remind_at: str | None = None  # 提醒时刻 (ISO8601)；None = 备忘录，有值 = 日程
    status: str = STATUS_ACTIVE  # active 还惦记 / done 做了 / dropped 划了
    noted_at: str = ""           # 她记下这条的现实时刻 (ISO8601)


async def note_entry(
    *,
    lane: str,
    persona_id: str,
    entry_id: str,
    content: str,
    remind_at: str | None,
    noted_at: str,
) -> None:
    """记一条（首写）→ ``insert_idempotent`` 落一条 ver=0 的 NotebookEntry。

    durable 幂等（对称 ``perform_act``）：整轮重试 / durable 重投用同一
    ``(lane, persona_id, entry_id)`` 再写一次，dedup_hash 折进 ver=0 → ON CONFLICT
    DO NOTHING、只落一条。``entry_id`` 由触发源派生（工具层负责），不让模型生成。

    ``remind_at`` 有值时校验是合法 ISO 时刻（脏串 fail-fast 抛 ValueError，工具层喂回
    模型重填）—— 排日程从首写就挡住脏时间，否则脏串落库后渲染 / 调度两边不一致。
    """
    if remind_at is not None:
        _validate_remind_at(remind_at)
    await insert_idempotent(
        NotebookEntry(
            lane=lane,
            persona_id=persona_id,
            entry_id=entry_id,
            content=content,
            remind_at=remind_at,
            status=STATUS_ACTIVE,
            noted_at=noted_at,
        )
    )


async def find_notebook_entry(
    *, lane: str, persona_id: str, entry_id: str
) -> NotebookEntry | None:
    """读单条 entry 的最新一版（改 / 划过取改后的），不存在返回 ``None``。

    单一来源（宪法「禁止重复定义」）：``update_entry`` 改前读、日程到点提醒 gate 判
    「这条现在还作不作数」（仍 active / remind_at 是否被改期 / 撤掉）都走这一处，不再
    各自 inline ``select_latest``。照 :func:`find_life_state` 的姿势薄封 ``select_latest``。
    """
    return await select_latest(
        NotebookEntry,
        {"lane": lane, "persona_id": persona_id, "entry_id": entry_id},
    )


async def update_entry(
    *,
    lane: str,
    persona_id: str,
    entry_id: str,
    content: str | None = None,
    remind_at: str | None = None,
    clear_remind_at: bool = False,
    status: str | None = None,
) -> None:
    """改 / 划一条 → 读最新一版、改给定字段、append 一版（ver 自增）。

    只传的字段才改，其余沿用最新一版（不被默认值清掉，对称 ``save_life_state`` 沿用
    next_wake_at 的命门）。``remind_at`` 有「补 / 撤」两个方向：

      * ``remind_at=<时刻>`` —— 设 / 改提醒时间（备忘 → 日程，或改期）。
      * ``clear_remind_at=True`` —— 把时间撤了（日程 → 备忘）。``remind_at=None`` 单独
        无法表达「撤」（None 是「没传、别动」的默认值），所以撤时间用独立的布尔信号。

    ``noted_at`` 是「她什么时候记下这条」的创建时刻，**改动不重写它**（沿用最新一版）：
    每次改 append 出的新版自带 framework 的 ``created_at`` 落库时刻，「她最近动过这条」
    的信息已落在版本链里，无需再用一个业务字段重复承载（不为未必需要的「最后改动时刻」
    预先抽象——业务代码不是 SDK）。

    改一条不存在的 entry → 抛 ``ValueError``（工具层 @tool_error 把它喂回模型重调，
    不静默造一条）。

    机制护栏（在读 DB 之前 fail-fast，脏入参根本不进版本链）：传了 ``status`` 时只允许
    三态、传了 ``remind_at`` 时必须是合法 ISO 时刻，非法各抛 ``ValueError`` 让模型重填
    （status 拼错会让条目静默失踪、remind_at 脏串会让渲染 / 调度不一致——见各校验函数）。
    校验在 ``find_notebook_entry`` 之前：脏入参连存在性检查都不必做、立刻喂回模型。
    """
    if status is not None:
        _validate_status(status)
    if remind_at is not None:
        _validate_remind_at(remind_at)
    prev = await find_notebook_entry(
        lane=lane, persona_id=persona_id, entry_id=entry_id
    )
    if prev is None:
        raise ValueError(
            f"本子里没有 entry_id={entry_id!r} 这条（lane={lane}, persona={persona_id}）。"
            "请先确认 id（翻本子能拿到），或这条还没记过。"
        )

    if clear_remind_at:
        new_remind_at: str | None = None
    elif remind_at is not None:
        new_remind_at = remind_at
    else:
        new_remind_at = prev.remind_at

    await insert_append(
        NotebookEntry(
            lane=lane,
            persona_id=persona_id,
            entry_id=entry_id,
            content=content if content is not None else prev.content,
            remind_at=new_remind_at,
            status=status if status is not None else prev.status,
            noted_at=prev.noted_at,
        )
    )


async def list_notebook_entries(
    *, lane: str, persona_id: str, active_only: bool
) -> list[NotebookEntry]:
    """翻本子：取她这个本子里每条的最新一版（每条只一行）。

    ``active_only=True`` 默认只列还活着的（status == active）；``False`` 列全部（含
    done / dropped，睡前清理 / 找旧条目时用）。照 ``list_relationship_pages`` 的先例
    在 framework 持久化写好的真实表上做只读 SELECT（DISTINCT ON 每条取最新一版）；写入
    仍走 ``insert_idempotent`` / ``insert_append``，不绕开 framework 持久化原语。
    """
    from sqlalchemy import text

    from app.data.session import get_session
    from app.runtime.migrator import _table_name

    sql = (
        f"SELECT DISTINCT ON (entry_id) * FROM {_table_name(NotebookEntry)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY entry_id ASC, ver DESC"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), {"lane": lane, "persona_id": persona_id})
        entries = [
            NotebookEntry(**{k: row[k] for k in NotebookEntry.model_fields})
            for row in r.mappings()
        ]
    if active_only:
        entries = [e for e in entries if e.status in ACTIVE_STATUSES]
    return entries


def entry_status_label(entry: NotebookEntry, now: str) -> str:
    """这条在呈现时显示的状态：还惦记 / 到点了 / 做了 / 划了。

    「到点了」是**派生显示**、不是存储态（spec：状态全是她自己的判断、没有任何定时器
    把日程标过期）——一条还 active 的日程，若它的 ``remind_at`` 已早于此刻 ``now``，
    呈现时标「到点了」让她一眼看出哪条过点了。remind_at / now 解析不出真实时刻（脏串）
    时退回「还惦记」（不静默猜过没过点）。
    """
    if entry.status == STATUS_DONE:
        return "做了"
    if entry.status == STATUS_DROPPED:
        return "划了"
    # active：有提醒时间且已过点 → 到点了；否则还惦记。
    if entry.remind_at:
        remind = cst_time.parse(entry.remind_at)
        cur = cst_time.parse(now)
        if remind is not None and cur is not None and remind <= cur:
            return "到点了"
    return "还惦记"


def render_notebook(entries: list[NotebookEntry], *, now: str) -> str:
    """把本子条目列表渲成给模型看的文字（每条带 id / 内容 / 时间 / 状态）。

    **单一定义处**（宪法「禁止重复定义」）：``read_notebook`` 工具、life 唤醒输入、chat
    inner_context 三处共用这一份渲染——本子是同一份内容，渲染只该有一处。

    空本子给一句提示（不报错、不返回空串让模型困惑）。每条一行，时间只在有 remind_at
    时出现（备忘没时间）。状态走 :func:`entry_status_label`（含派生的「到点了」）。
    """
    if not entries:
        return "本子是空的，还没记过什么。"
    lines = []
    for e in entries:
        label = entry_status_label(e, now)
        time_part = f"，提醒 {e.remind_at}" if e.remind_at else ""
        lines.append(f"[{e.entry_id}] {e.content}{time_part}（{label}）")
    return "\n".join(lines)
