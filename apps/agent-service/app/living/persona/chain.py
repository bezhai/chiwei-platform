"""「她是谁」那份正文 —— 存在哪、谁写、谁读它（persona 插件的 ``persona`` 服务）。

**她是谁只有 bezhai 写。** ``bot_persona.persona_core`` 是写死的出厂快照，全仓没有任何代码往它里面
写；bezhai 改她的时候在 :class:`PersonaVersion` 这条 framework Data 版本链上手写 SQL 追加一版
（``source='owner'``，Key + 正文 + 写下时刻 + Version、append-only、读最新一版、整篇取代旧版）。代码
不往这条链里写。主表不动，链上一版都没有时读它。

**读侧必须收口在这里。** 喂人设进模型的地方有四处（她的一轮、写日记、每周回看、读书），各自
去查一遍主表的话，链上那些版本一个字都到不了她眼前——而且**一句报错都没有**，每一轮照跑，
只是底色永远停在出厂那份。所以它们都经 :class:`PersonaChain`（persona 插件交出去的服务）
取变量，调用方只给 ``lane`` 和 ``persona_id``，没有第二个组装点。

每版带来源 ``source``：现在只有 ``owner``（bezhai 手写）一种还在写。链上更早的那些 ``seed``（链空时
把主表原文灌成第一版）和 ``review``（每周回看整篇重写的版本）是之前的代码写下的，原样留着；读最新一版
不分来源。每周回看现在写的是另外一段「这阵子」（:mod:`app.living.persona.notes`），不碰这条链。

自然键带 ``lane``：漏了它的后果是双向的，coe 里改出来的人设会当场生效在 prod 的她身上，而库里看不出
异常——两条链都在，只是读的时候挑错了行。

读最新走 framework 的 ``select_latest``。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Annotated

from app.agent.round import Round
from app.data.persist import select_latest
from app.data.queries import find_persona
from app.data.schema import Data, Key, Version
from app.infra.lane_policy import current_deployment_lane
from app.living.common import week_start_cst
from app.living.persona.notes import WeeklyNote, append_note, read_latest_note

logger = logging.getLogger(__name__)

# 读书那份人设的进程内缓存（秒）：周级慢漂，5 分钟不失效也看不出差别。
LITE_TTL_SECONDS = 300


class PersonaVersion(Data):
    """「她是谁」身份正文的自然语言全文快照（一版）.

    自然键 ``(lane, persona_id)``：泳道隔离 + 每个角色一条链。``narrative`` 是身份正文全文
    ——与 ``bot_persona.persona_core`` 同族口吻，注入方零适配。``source`` 是这一版从哪来
    （现在只写 owner，seed / review 是历史版本，见模块 docstring）。
    ``written_at`` 是写下这版的现实时刻（**CST ISO8601 字符串，不是 datetime**）：
    命名避开框架保留列 ``created_at``（那是框架的落库时刻，语义不同，同 LivingDayPage 教
    训），类型也不改——这张表 prod 上已经有几十版真实数据，migrator 是 additive-only，
    改列类型 / 删列直接 ``MigrationError``、整批迁移回滚、pod crash loop。
    ``version`` 让多版正文 append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    narrative: str    # 身份正文全文
    source: str       # 这版从哪来：owner（bezhai 手写）；seed / review 是之前代码写下的历史版本
    written_at: str   # 写下这版的现实时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def read_latest_version(*, lane: str, persona_id: str) -> PersonaVersion | None:
    """最新一版身份正文，**不分来源**——bezhai 手写的那一版即生效；没有返回 None
    （冷启：读侧 fallback ``bot_persona`` 主表）。

    "最新"按 ``version`` 算，不按 ``written_at``：后者是手写进去的字符串，可以填任何时刻，
    按它排序的话一次填错时刻就让链永远停在那一版上。
    """
    return await select_latest(
        PersonaVersion, {"lane": lane, "persona_id": persona_id}
    )


def _persona_core_var(*candidates: str | None) -> str:
    """SYSTEM 变量 ``{{persona_core}}`` 的值：第一份写下来了的正文；都没有就说实话。

    候选按可信度排：链上最新一版 → ``bot_persona.persona_core``。有就**原文直传、不
    裹任何措辞**。全都空白（两列都 NOT NULL，但可以是空串；owner 也可能人工写进一段
    空白）绝不能渲染出一个空洞——她 90% 时间在 rest 的第二个独立病因就是这份东西全仓
    只有每周一次的 persona_review 读过：不是想做事做不了，是根本没想起来自己有想做
    的事。

    缺省措辞零剧情事实：人设内容全部从数据来。
    """
    for core in candidates:
        if core and core.strip():
            return core.strip()
    return "（还没有为她写下这份人设正文——这一轮没有可对照的底色。）"


# 她还没写过「这阵子」时 ``{{recent_note}}`` 的值：说实话，不留一个空洞。
NO_NOTE_YET = "你还没有回头看过哪一周。"


def _recent_note_var(note: WeeklyNote | None) -> str:
    """SYSTEM 变量 ``{{recent_note}}`` 的值：她最近写下的那一段，前面一行说清是哪一周过完时写的；还没写过
    就是 :data:`NO_NOTE_YET`。

    周写成"那一周的周一到周日"（``week_until`` 是下一周的周一，右开，所以减一天）。不写"上周"：她读到
    这一段的时候可能已经隔了几天，甚至隔了一周（那一周回看没写成），说"上周"就是一句假话。
    """
    if note is None:
        return NO_NOTE_YET
    last_day = note.week_until - timedelta(days=1)
    return (
        f"（{note.week_since:%Y-%m-%d} 到 {last_day:%Y-%m-%d} 那一周过完，你回头看时写下的）\n"
        f"{note.note}"
    )


class PersonaChain:
    """persona 服务（:class:`app.living.services.Persona`）：「她是谁」的全部读写都经这里。

    唯一的状态是读书那份人设的进程内缓存（:meth:`lite_vars`）：一个进程一个对象，persona 插件
    在 setup 里建它。
    """

    def __init__(self) -> None:
        self._lite: dict[str, tuple[dict[str, str], float]] = {}

    async def prompt_vars(self, *, lane: str, persona_id: str) -> dict[str, str]:
        """她的一轮、写日记时 SYSTEM 里的那一段：她是谁（:meth:`who_she_is`），和她最近一次回头看时
        写下的「这阵子」（``recent_note``，:func:`_recent_note_var`）。**恰好三个**，全仓一处组装。

        **只有不随轮次变的东西才配当 prompt 变量**：她叫什么、她是个什么样的人、她这阵子怎么样（一周才
        换一次）。每一轮都变的（快照、手机信封、那一天的材料）一律走 USER 消息，理由是 prompt 变量没有
        编译期校验、改名会静默渲染成字面量，能少一个就少一个；SYSTEM 也因此只在她写下新的一段「这阵子」
        时变一次，前缀缓存照样命中。同理这几个键名就是 Langfuse 上那几个 prompt 正文里写着的名字，改代码
        这边等于让它们原样渲染成字面量出现在她眼前。
        """
        who = await self.who_she_is(lane=lane, persona_id=persona_id)
        note = await read_latest_note(lane=lane, persona_id=persona_id)
        return {**who, "recent_note": _recent_note_var(note)}

    async def who_she_is(self, *, lane: str, persona_id: str) -> dict[str, str]:
        """她是谁：``{persona_name, persona_core}``。每周回看只要这两个：那一段「这阵子」要只出自这一周
        别人那一侧的事，她上一次写的那段不能流回去。

        正文取链上最新一版（不分来源，bezhai 手写的那一版即刻生效），链空 / 那一版空白才退回主表
        的 ``persona_core``，两边都空白退回 :func:`_persona_core_var` 里那句实话。

        库里没有这个 persona 时不让这一轮跑不起来：``getattr`` 取而不是断言字段在，名字
        退回 ``persona_id``，正文走同一条退化链。
        """
        persona = await find_persona(persona_id)
        latest = await read_latest_version(lane=lane, persona_id=persona_id)
        return {
            "persona_name": getattr(persona, "display_name", "") or persona_id,
            "persona_core": _persona_core_var(
                latest.narrative if latest is not None else None,
                getattr(persona, "persona_core", None),
            ),
        }

    async def identity_vars(self, round: Round) -> dict[str, str]:
        """她一轮 prompt 里「她是谁」那一段的三个变量，一轮开始取齐 prompt 时算一次
        （:meth:`prompt_vars`，这一轮的泳道和她）。"""
        return await self.prompt_vars(lane=round.lane, persona_id=round.participant)

    async def lite_vars(self, *, persona_id: str) -> dict[str, str]:
        """读书 agent 的两个 prompt 变量 ``{persona_name, persona_lite}``，进程内缓存
        :data:`LITE_TTL_SECONDS`。

        ``persona_lite`` 优先取版本链最新一版（不分来源——bezhai 手写的那一版即生效），链空 / 链最新版
        空白 / 链读失败退回 ``bot_persona.persona_lite``；名字永远来自主表。主表没这一行时名字
        退回 ``persona_id``、正文是空串，链也不查（链以主表为源）。lane 口径 =
        ``current_deployment_lane() or "prod"``。周级漂移 + 5 分钟 TTL，无需任何失效机制。
        """
        now = time.monotonic()
        cached = self._lite.get(persona_id)
        if cached is not None and now < cached[1]:
            return dict(cached[0])

        persona = await find_persona(persona_id)
        if persona:
            narrative = await self._lite_from_chain(persona_id)
            lite = {
                "persona_name": persona.display_name,
                "persona_lite": (
                    narrative if narrative is not None else persona.persona_lite or ""
                ),
            }
        else:
            lite = {"persona_name": persona_id, "persona_lite": ""}

        self._lite[persona_id] = (lite, now + LITE_TTL_SECONDS)
        return dict(lite)

    async def _lite_from_chain(self, persona_id: str) -> str | None:
        """版本链最新一版的身份正文；链空 / 链最新版空白 / 读失败返回 None（退回主表）。

        读失败只 log 不抛——读书绝不能因为人设注入塌掉。链上最新版正文空白时按链空处理：
        链只有手写这一个入口，写进一段空白的可能是有的。
        """
        try:
            lane = current_deployment_lane() or "prod"
            latest = await read_latest_version(lane=lane, persona_id=persona_id)
        except Exception as e:
            logger.warning(
                "[%s] Failed to read persona version chain: %s", persona_id, e
            )
            return None
        if latest is None:
            return None
        if not latest.narrative or not latest.narrative.strip():
            logger.warning(
                "[%s] Latest persona version narrative is blank, "
                "falling back to bot_persona main table",
                persona_id,
            )
            return None
        return latest.narrative

    async def note_written_this_week(
        self, *, lane: str, persona_id: str, now: datetime
    ) -> bool:
        """``now`` 所在的自然周（周一 00:00 CST 起）她写过「这阵子」没有
        （:class:`app.living.persona.notes.WeeklyNote`，看最近那一段的 ``written_at``）。

        每周回看的幂等只看这一张表，不另设标记：标记和那一段是两个事实，中间崩一次就永久对不上。
        """
        latest = await read_latest_note(lane=lane, persona_id=persona_id)
        return latest is not None and latest.written_at >= week_start_cst(now)

    async def write_note(
        self,
        *,
        lane: str,
        persona_id: str,
        note: str,
        week_since: date,
        week_until: date,
        written_at: datetime,
    ) -> None:
        """追加一段「这阵子」（:func:`app.living.persona.notes.append_note`）。"""
        await append_note(
            lane=lane,
            persona_id=persona_id,
            note=note,
            week_since=week_since,
            week_until=week_until,
            written_at=written_at,
        )
