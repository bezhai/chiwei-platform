"""读东西 —— 她读的是别人发到她手机上的一个文件，没有"书架"这回事。

**没有"书"这个注册物。** 她能读的严格等于她手机上收到过的文件（``common_message``
里一条普通的 file 项，和图片走同一条媒体轨）。所以"读什么"这个问题的答案只能从她
的会话里来：同一条口径的会话集合（``common_bot_presence`` + 她自己的 bot），跟
:mod:`app.living.phone` 认"哪些会话在她手机上"是同一条 —— 她读得到的严格等于她收
得到的。

**认不准是哪个文件就回问，不替她选。** 同一本书被两个人各发过一次是常事，而这两份
是**两个不同的东西**：附件实例身份由「收到它那一次」派生（``derive_attachment_id``
= 消息 id + file_key），重发一次就是另一条印象链，两份印象不合并。所以一个名字对上
好几份时，工具把候选连着来路（谁发的、什么时候、发在哪条会话）一起摊开让她指，
**绝不排个序取第一个** —— 读哪一份是她的事。回问里给的那串 ``file=...`` 再报一次
就能开读，所以"让她指"是真指得动的，不是一句空话。

**一程是异步的：她拿起来，然后这一缝就过去了。** 工具只 emit 一个 durable
:class:`FilePickedUp`，读那一程在别处跑（读一页要取字节、解码、几轮模型调用，塞进
一缝里会把她卡在网络上）。这跟她说出去的话要等下一缝才知道下场是同一个形状。

**读到哪了、读出什么，落在 :class:`FileRead` 上，一个文件一条版本链。** 不复用旧
引擎的 ``BookImpression``：那张表的 ``status`` 有"放下"这一档，而新引擎里"放下一本
书"根本不是一个她能做的动作（她只是不再拿起它），带着一个永远写不进去的取值是在
描述一个不存在的状态机。新引擎自己的 durable 记录一律住在 ``app/living/``
（``Happening`` / ``LooseEnd`` / ``PhoneRead`` / ``SpokenOutbound``），这条不破例。

**一程读不成，印象一个字都不动。** 取不到字节（没缓存进对象存储 / 预签失败 / GET
挂了）、解码失败、超时、空产出，:func:`app.agent.reading.run_reading_round` 一律返回
``None``，这里据此**不提交**：印象和读到第几页原样停在上一程的位置，她下次自己想读
了再拿起来一次。绝不写半截脏印象 —— 空印象覆盖整篇就是真失忆。

**同一程递两次不能读两遍。** durable 边本身按 ``(lane, round_id)`` 去重，但那层去重
的租约会过期、过期后另一个 worker 会接手；接手时上一次可能已经提交过了。所以这里
还有第二道：跑昂贵的那一程之前先看当前印象上的 ``round_id`` 是不是就是这一程 ——
是就跳过。提交本身走版本 CAS，拿着过时印象的那一程（并发 / 重放）写不进去，页号
不会被推两次。

**上限只管她还没翻开过的那摞**（:data:`FILE_LIST_LIMIT`）。读过的一条都不省 ——
印象是她的记忆，被条数上限截掉就是替她遗忘。而没翻开过的那摞被挤下去也不是永久
丢失：报名字那条路一个上限都没有，很久以前收到的那个照样指得到。

取字节、解码、分页、跑一程阅读这几样在 :mod:`app.domain.reading_source` 和
:mod:`app.agent.reading` 里，跟哪个引擎在跑没有关系，直接用。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import Field, field_validator
from sqlalchemy import text

# module-level 引入，测试从这里换替身（一程真跑起来要取字节 + 几轮模型调用）
from app.agent.reading import run_reading_round
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.data.session import get_session
from app.domain.reading_source import derive_attachment_id, derive_tos_file
from app.infra import cst_time
from app.infra.cst_time import CST, to_cst_dated
from app.living.records import _require_aware
from app.living.scope import moment_scope
from app.runtime.data import Data, Key, Version
from app.runtime.emit import emit
from app.runtime.migrator import _table_name
from app.runtime.node import node
from app.runtime.persist import insert_append, select_latest

logger = logging.getLogger(__name__)

# 一次给她列几个**还没翻开过**的文件。读过的不受它管（见模块 docstring）。一屏能
# 摆多少是显示口径，不是"她该记得几个"——报名字那条路没有上限，被挤下去的照样指得到。
FILE_LIST_LIMIT = 10

# 派生一程身份的命名空间。换掉它 == 同一缝里重复拿起会派生出两程。
_ID_NS = uuid.UUID("6f2a5d38-9b41-4c07-a5e2-3d81b6f0c974")


class FileRead(Data):
    """她读一个文件读到哪了、这个文件此刻在她心里是什么样。

    自然键 ``(lane, persona_id, attachment_id)``，有版本链：每读完一程 append 一版
    （整篇覆盖重写印象 + 推进页号），读取取最新一版，旧版留着不删。``lane`` 必须
    进 Key —— runtime 不给任何 Data 自动加 lane，不显式带就会写脏 prod 那条轴。

    ``attachment_id`` 是**附件实例**的身份，由收到该文件那一次派生
    （:func:`app.domain.reading_source.derive_attachment_id`），opaque、任何地方不
    反解。同一本书被重发一次就是另一个 attachment_id、另一条版本链，两份印象各自
    独立 —— 拿内容 hash 当身份会把它们合并成一份，那就分不出"她读的是谁发的那份"。

    ``impression`` 是**整篇覆盖重写**的第一人称印象，不是章节梗概：读得多了早先的
    会糊、近的会清楚，这正是一个人读完一本书之后剩下的东西。选它而不是结构化档案
    （人物表 / 索引），是因为后者会把她变成读取机器。

    ``pages_read`` 是"下次从第几页接着读"，由一程里真读到的连续前沿派生，**不从
    模型自报的页号抠**（分页是读时现算的定长切分，确定性、跨程稳定）。

    ``finished`` 只记"读到结尾了"这一件客观事。**没有"放下"这一档**：不再读一个
    东西不是一个动作，是她不再拿起它；给它设一个状态位就得有人去写它，而那个人
    只能是替她做决定的代码。读完了她想再翻开也随她，这里不拦。

    ``round_id`` 是提交这一版的那一程的身份，:func:`read_a_round` 跑昂贵的那一程
    之前拿它查重（同一程递两次不读两遍）。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    attachment_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    title: str           # 她眼里这东西叫什么（= 收到时的原始文件名）
    impression: str      # 第一人称滚动印象，整篇覆盖重写
    pages_read: int      # 下次从第几页接着读
    finished: bool       # 读到结尾了吗
    read_at: datetime    # 这一版写下的时刻
    round_id: str        # 提交这一版的那一程

    @field_validator("read_at")
    @classmethod
    def _aware_read_at(cls, v: datetime) -> datetime:
        return _require_aware("read_at", v)


class FilePickedUp(Data):
    """她在某一缝拿起了一个文件要读一程 —— 交给别处去读的那个信号。

    自然键 ``(lane, round_id)``；``round_id`` 从 ``(lane, persona, 这一缝, 这个附件
    实例)`` 派生，所以工具重试、整轮重放、这一缝重跑全撞同一程，durable 边按它去重、
    不重复读。下一缝是新的 ``moment_id`` = 新的一程 —— **要不要接着读是她的决定**，
    不是系统替她排的下一次。

    ``title`` 一样东西两个用处：给她看（她眼里这文件叫什么），以及解码时按后缀分流
    （``.epub`` 走 epub 解析、其余按 txt 读）。分流必须靠原始文件名 —— ``file_key``
    不保证带后缀。

    ``tos_file`` 是对象存储引用（``files/<file_key>``），由 file_key 确定性派生、
    不等任何回填（那条回填机制是 image-only、对文件根本不跑）。读那一程按它取字节。
    """

    lane: Annotated[str, Key]
    round_id: Annotated[str, Key]
    persona_id: str
    attachment_id: str
    title: str
    tos_file: str


_READ_TABLE = _table_name(FileRead)


# ---------------------------------------------------------------------------
# 有人发给她的那些文件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SentFile:
    """有人发到她手机上的一个文件：叫什么、谁发的、什么时候、发在哪条会话。

    来路（``who`` / ``at`` / ``where``）不是装饰：同名多份时它们是她**唯一**能
    用来分辨"哪一份是哪一份"的东西。少了它们，回问就只能给两串一模一样的名字。
    """

    attachment_id: str
    title: str          # 原始文件名，可能是空的（发的人那边就没给名字）
    tos_file: str
    who: str
    at: datetime
    where: str


# 会话集合跟 :data:`app.living.phone._REACHABLE_SQL` 同一条口径（她自己的 bot 还在
# 的那些）：她读得到的严格等于她收得到的。
#
# 文件项的字段名是 ``kind`` / ``key``（``meta.file_name`` 放原始文件名），跟渠道投影
# 写进 ``common_message.content`` 的形状一致 —— 见
# ``tests/living/test_reading.py`` 里那份照抄真实记录的常量。**曾经写成
# ``type`` / ``value``**：SQL 和测试数据用了同一份臆造形状、彼此自洽所以全绿，而线上
# 一个文件都查不出来，她永远只会说"没有谁给你发过什么可以读的东西"。改这几个字段名
# 之前先去库里看一条真实记录。
#
# **不加时间窗。** 加了她就再也读不到上个月别人发来的那本书 —— 那正是"阈值替她
# 遗忘"。代价是全表扫，用 ``content @> '[{"kind":"file"}]'`` 先把绝大多数消息挡在
# 展开之前（jsonb 包含判断，比逐条展开数组便宜得多）。展开时仍要挡一次
# ``jsonb_typeof = 'array'``：content 不是数组的历史行会让 jsonb_array_elements 直接
# 报错，同 phone 里那几条查询的写法。
#
# ``DISTINCT`` 是必需的：一个 persona 名下可能挂着好几个 bot（正式那个和 dev 那个），
# 同一条会话会被 presence 匹配出好几行，不去重同一个文件就会列出来好几遍。
_SENT_FILES_SQL = """
WITH hers AS (
  SELECT DISTINCT
         cc.common_conversation_id AS channel_id,
         cc.scope                  AS scope,
         COALESCE(cc.display_name, '') AS title
    FROM common_bot_presence bp
    JOIN bot_config bc
      ON bc.bot_name = bp.bot_name
     AND bc.persona_id = :persona_id
     AND bc.is_active = true
    JOIN common_conversation cc
      ON cc.common_conversation_id = bp.common_conversation_id
     AND cc.is_active = true
   WHERE bp.is_active = true
)
SELECT DISTINCT
       CAST(cm.common_message_id AS text)       AS message_id,
       it->>'key'                               AS file_key,
       COALESCE(it->'meta'->>'file_name', '')   AS file_name,
       COALESCE(cm.sender_display_name, '某人') AS who,
       cm.event_time                            AS at_ms,
       h.scope                                  AS scope,
       h.title                                  AS where_title
  FROM common_message cm
  JOIN hers h ON h.channel_id = cm.common_conversation_id
 CROSS JOIN LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(cm.content) = 'array'
            THEN cm.content ELSE '[]'::jsonb END
 ) AS it
 WHERE cm.content @> '[{"kind": "file"}]'::jsonb
   AND it->>'kind' = 'file'
   AND COALESCE(it->>'key', '') <> ''
 ORDER BY at_ms DESC, message_id DESC
"""


def _where_of(scope: str, title: str, who: str) -> str:
    """这个文件发在哪儿 —— 私聊多半没有会话名，那就用发的人当它的名字。

    跟 :func:`app.living.phone.look_up_contact` 同一条处理：在她眼里那条私聊本来
    就叫那个人。
    """
    label = title or who
    return f"群「{label}」" if scope != "direct" else f"私聊「{label}」"


async def files_sent_to(*, persona_id: str) -> list[SentFile]:
    """有人发到她手机上的全部文件，最近的在前。查不到就是查不到，不猜、不兜底。"""
    async with get_session() as s:
        rows = (
            await s.execute(text(_SENT_FILES_SQL), {"persona_id": persona_id})
        ).mappings().all()
    return [
        SentFile(
            attachment_id=derive_attachment_id(
                common_message_id=str(r["message_id"]), file_key=str(r["file_key"])
            ),
            title=r["file_name"],
            tos_file=derive_tos_file(str(r["file_key"])),
            who=r["who"],
            at=datetime.fromtimestamp(int(r["at_ms"]) / 1000, tz=CST),
            where=_where_of(r["scope"], r["where_title"], r["who"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 读到哪了
# ---------------------------------------------------------------------------


async def read_so_far(
    *, lane: str, persona_id: str, attachment_id: str
) -> FileRead | None:
    """她这个文件读到哪了（最新一版）；从没翻开过返回 ``None``。"""
    return await select_latest(
        FileRead,
        {"lane": lane, "persona_id": persona_id, "attachment_id": attachment_id},
    )


async def everything_read_so_far(
    *, lane: str, persona_id: str
) -> dict[str, FileRead]:
    """她读过的每个文件的最新一版，按附件实例身份索引。

    一次查回来而不是逐个文件查：列清单时每个文件都要问一句"读到哪了"，逐个查就是
    列几个文件查几次库。
    """
    sql = (
        f"SELECT DISTINCT ON (attachment_id) * FROM {_READ_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY attachment_id, ver DESC"
    )
    async with get_session() as s:
        rows = (
            await s.execute(text(sql), {"lane": lane, "persona_id": persona_id})
        ).mappings().all()
    marks = [FileRead(**{k: r[k] for k in FileRead.model_fields}) for r in rows]
    return {m.attachment_id: m for m in marks}


# ---------------------------------------------------------------------------
# 两只手：看看有什么能读的 / 拿起来读一程
# ---------------------------------------------------------------------------


def _name_of(f: SentFile) -> str:
    """给她看的那个名字。发的人那边就没给名字时如实说没有（只能靠 file= 那串指它）。"""
    return f"《{f.title}》" if f.title else "（一个没有名字的文件）"


def _one_file(f: SentFile, mark: FileRead | None, *, now: datetime) -> str:
    """一个文件在她眼里的样子：叫什么、谁什么时候发在哪儿、她读到哪了、记得什么。

    时刻走 :func:`app.infra.cst_time.to_cst_dated` 而不是裸时分：几个月前收到的
    文件和今天刚收到的，裸 ``20:10`` 长得一模一样。
    """
    when = to_cst_dated(f.at.isoformat(), now=now, seconds=False)
    head = (
        f"- {_name_of(f)} {f.who} {when} 发在{f.where} "
        f"file={f.attachment_id}"
    )
    if mark is None:
        return head + "\n  （还没翻开过）"
    read = "你读完了" if mark.finished else f"你已经读了 {mark.pages_read} 页"
    return f"{head}\n  {read}。这个东西在你心里：{mark.impression}"


@tool
@tool_error("看看有什么能读的失败")
async def look_for_something_to_read() -> str:
    """看看有人给你发过什么能读的东西，还有你读过的那些现在在你心里是什么样。

    别人发到你手机上的文件都在这儿，每个带一串 file=...，拿它调 read_a_bit 就能
    读一程。你读过的那些会连着你读到哪了、它在你心里留下了什么一起给你。

    没翻开过的一次只列最近那些；你读过的一个都不会少。

    Returns:
        有人发给你的那些文件，各带一串 file=...；一个都没有时如实说明。
    """
    lane, now, persona_id, _moment_id = moment_scope()
    files = await files_sent_to(persona_id=persona_id)
    if not files:
        return "没有谁给你发过什么可以读的东西。"

    marks = await everything_read_so_far(lane=lane, persona_id=persona_id)
    # 读过的一条都不省，上限只管还没翻开过的那摞（见模块 docstring）。被挤下去的
    # 那些没有消失：报名字那条路没有上限。
    opened = [f for f in files if f.attachment_id in marks]
    unopened = [f for f in files if f.attachment_id not in marks]
    skipped = max(0, len(unopened) - FILE_LIST_LIMIT)
    shown = opened + unopened[:FILE_LIST_LIMIT]

    lines = ["有人发给你的（想读就拿起来读一程）："]
    lines += [
        _one_file(f, marks.get(f.attachment_id), now=now) for f in shown
    ]
    if skipped:
        lines.append(
            f"还有 {skipped} 个没在这儿列出来 —— 想得起名字就直接报名字。"
        )
    return "\n".join(lines)


@tool
@tool_error("拿起这个文件失败")
async def read_a_bit(
    which: Annotated[
        str,
        Field(
            description="你要读的那个文件叫什么，记得多少写多少；"
            "也可以直接写清单里那串 file=... 后面的东西"
        ),
    ],
) -> str:
    """拿起有人发给你的一个文件，往下读一程。

    报名字就行，记得多少写多少。名字对上好几份（同一本书两个人各发过一次）时，
    它会把这几份连着谁发的、什么时候发的一起摆给你，**它不会替你挑**——你自己认
    哪一份，把那串 file=... 报回来。

    读读停停都随你：这一程读到哪儿是你自己停下的地方，下次拿起同一个文件就从那儿
    接着往下。

    你读完一程要过一会儿才在你心里 —— 想知道你读到哪了、它给你留下了什么，
    调 look_for_something_to_read。

    Args:
        which: 你要读的那个文件的名字，或者那串 file=... 后面的东西。

    Returns:
        你拿起了哪个文件的一句确认；没有对得上的 / 对上好几份时一句如实说明。
    """
    lane, now, persona_id, moment_id = moment_scope()
    wanted = which.strip()
    if not wanted:
        raise ValueError("你没说要读哪个：写一个名字，或者那串 file=... 后面的东西。")

    files = await files_sent_to(persona_id=persona_id)
    # 一条判据两条腿：报的是那串身份（照抄回来的），或者是名字的一部分。**不排序、
    # 不取第一个** —— 命中几个就是几个，认不准由她自己认。
    needle = wanted.casefold()
    hit = [
        f
        for f in files
        if f.attachment_id == wanted or (f.title and needle in f.title.casefold())
    ]

    if not hit:
        raise ValueError(
            f"没有谁给你发过名字对得上「{wanted}」的东西。换个说法，"
            f"或者用 look_for_something_to_read 看看你手上都有什么。"
        )
    if len(hit) > 1:
        # 同名多份：这几份是**不同的东西**（各自一条印象链），所以只能她自己认。
        # 摊开来路让她指得动，绝不替她挑一个。
        spread = "\n".join(
            f"- {_name_of(f)} {f.who} "
            f"{to_cst_dated(f.at.isoformat(), now=now, seconds=False)} "
            f"发在{f.where} file={f.attachment_id}"
            for f in hit
        )
        raise ValueError(
            f"「{wanted}」对上了好几份，它们是不同的东西：\n{spread}\n"
            f"你要读哪一份？把那串 file=... 后面的东西报回来。我不替你挑。"
        )

    picked = hit[0]
    # 一程的身份从这一缝 + 这个附件实例派生：这一缝里重复拿起（工具重试、整轮重放）
    # 撞同一程，下一缝才是新的一程。
    round_id = uuid.uuid5(
        _ID_NS,
        f"{lane}\x1f{persona_id}\x1f{moment_id}\x1f{picked.attachment_id}",
    ).hex
    await emit(
        FilePickedUp(
            lane=lane,
            round_id=round_id,
            persona_id=persona_id,
            attachment_id=picked.attachment_id,
            title=picked.title,
            tos_file=picked.tos_file,
        )
    )
    # 只陈述客观的那一件：你拿起来读了。读出什么感受是她读出来的，不在这儿替她编。
    return (
        f"你拿起{_name_of(picked)}读了起来。读进去要一会儿 —— "
        f"读到什么、它给你留下什么，得等你自己读完这一程。"
    )


READING_TOOLS = [look_for_something_to_read, read_a_bit]


# ---------------------------------------------------------------------------
# 读那一程（在别处跑）
# ---------------------------------------------------------------------------


@node
async def read_a_round(picked: FilePickedUp) -> None:
    """读一程：查重 → 取字节、解码、往下读 → CAS 提交读到哪了 + 印象。

    三道都是"错了就静默"的那种：

      1. **跑之前先查这一程提交过没有。** durable 边按 ``(lane, round_id)`` 去重，
         但那层的租约会过期、过期后另一个 worker 接手，而接手时上一次可能已经提交
         过了。没有这一道就是白烧一次钱、而且页号被推两次（她跳过了中间那几页，
         一句报错都没有）。
      2. **提交走版本 CAS。** 拿着过时印象的那一程（并发 / 重放）写入被拒，不会用
         旧印象盖掉新的、也不会把页号回退。
      3. **读不成就什么都不动。** 取不到字节 / 解码失败 / 超时 / 空产出，
         :func:`run_reading_round` 返回 ``None``，这里不提交 —— 印象和页号原样停在
         上一程的位置，她下次自己想读了再拿起来一次。
    """
    lane, persona_id = picked.lane, picked.persona_id
    attachment_id = picked.attachment_id

    current = await read_so_far(
        lane=lane, persona_id=persona_id, attachment_id=attachment_id
    )
    if current is not None and current.round_id == picked.round_id:
        logger.info(
            "living reading lane=%s persona=%s file=%s round=%s 这一程已经提交过了，"
            "不再读一遍",
            lane, persona_id, attachment_id, picked.round_id,
        )
        return

    prior = current.impression if current is not None else None
    start_page = current.pages_read if current is not None else 0
    # CAS 基线：从没读过时是 0（对齐 insert_append 的 COALESCE(MAX(ver), 0)）。
    expected_ver = current.ver if current is not None else 0

    result = await run_reading_round(
        lane=lane,
        persona_id=persona_id,
        attachment_id=attachment_id,
        book_title=picked.title,
        tos_file=picked.tos_file,
        file_name=picked.title,
        prior_impression=prior,
        start_page=start_page,
        round_id=picked.round_id,
    )
    if result is None:
        logger.info(
            "living reading lane=%s persona=%s file=%s round=%s 这一程没读成，"
            "印象和页号都不动（她可以再读一次）",
            lane, persona_id, attachment_id, picked.round_id,
        )
        return

    written = await insert_append(
        FileRead(
            lane=lane,
            persona_id=persona_id,
            attachment_id=attachment_id,
            title=picked.title,
            impression=result.impression,
            pages_read=result.pages_read,
            finished=result.finished,
            read_at=cst_time.now_cst(),
            round_id=picked.round_id,
        ),
        expected_current_ver=expected_ver,
    )
    if not written:
        # CAS 落败 = 期间有人写过一版（并发 / 过期任务抢先）。这正是 CAS 要拦的那条
        # 路：这一程作废，不炸，她可以再读一次。
        logger.info(
            "living reading lane=%s persona=%s file=%s round=%s 提交时印象已经变了"
            "（expected_ver=%d），这一程作废",
            lane, persona_id, attachment_id, picked.round_id, expected_ver,
        )
        return

    logger.info(
        "living reading lane=%s persona=%s file=%s round=%s 读到 %d 页%s",
        lane, persona_id, attachment_id, picked.round_id, result.pages_read,
        "，读完了" if result.finished else "",
    )
