"""「她是谁」那份正文 —— 写在哪、怎么变、谁读它（persona 插件的 ``persona`` 服务）。

**她是谁不是配置，是一条会变的链。** ``bot_persona.persona_core`` 是写死的出厂快
照，全仓没有任何代码往它里面写；她这几个月长出来的东西全落在 :class:`PersonaVersion`
这条 framework Data 版本链上（Key + 正文 + 写下时刻 +
Version、append-only、读最新一版、整篇重写——新版**取代**旧版）。主表不动，只当 v0
的来源和冷启 fallback。

**读侧必须收口在这里。** 喂人设进模型的地方有四处（她的一轮、写日记、每周回看、读书），各自
去查一遍主表的话，链上那些版本一个字都到不了她眼前——而且**一句报错都没有**，每一轮照跑，
只是底色永远停在出厂那份。所以它们都经 :class:`PersonaChain`（persona 插件交出去的服务）
取变量，调用方只给 ``lane`` 和 ``persona_id``，没有第二个组装点。三种取法各答一个问题，见
:class:`app.living.services.Persona`。

每版带来源 ``source``，一条链同时承担三件事、不拆来源就互相污染：

  * ``seed``  —— 出厂灌入：链为空时把 ``bot_persona.persona_core`` 原文落为第一版
    （:meth:`PersonaChain.seed_chain`，幂等、重跑无害）。灌的是 ``persona_core`` 而不是
    ``persona_lite``，因为链空时 :meth:`PersonaChain.prompt_vars` 退回的就是它——起点那
    一版必须等于她当时实际读到的那份。
  * ``review`` —— 自动慢漂：周级 review 写的版本。**自动班的幂等
    （:meth:`PersonaChain.has_review_this_week`）与证据游标
    （:func:`read_latest_review_written_at`）都只认它**。
  * ``owner`` —— bezhai 干预：人工盖版。读路径（:func:`read_latest_version`
    不分来源）即刻生效，但既不挡当周自动班、也不推走证据窗口。

这三个字符串是 prod 那条链上已经写着的值，改名等于把历史版本的来源判定全部作废。

周界 = **自然周一 00:00 CST**（:func:`app.living.common.week_start_cst`）。生活日是 04:00 界，但
persona 慢漂是周级的钟，周界用自然周一零点，两个口径不混。

自然键带 ``lane``：漏了它的后果是双向的，coe 里跑实验改出来的人设会当场生效在 prod
的她身上，而库里看不出异常——两条链都在，只是读的时候挑错了行。

写入走 framework 的 ``insert_append``（Version 自增），读最新走 ``select_latest``；
按来源过滤是 framework 没提供的只读查询，照 day_page_exists 的先例在 framework 持久
化写好的真实表上直接 SELECT——不绕开 framework 持久化原语。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated

from app.agent.round import Round
from app.data.persist import fetch_value, insert_append, select_latest
from app.data.queries import find_persona
from app.data.schema import Data, Key, Version, table_name
from app.infra.cst_time import now_cst, now_cst_iso
from app.infra.cst_time import parse as parse_time
from app.infra.lane_policy import current_deployment_lane
from app.living.common import week_start_cst
from app.living.services import PersonaText

logger = logging.getLogger(__name__)

# 读书那份人设的进程内缓存（秒）：周级慢漂，5 分钟不失效也看不出差别。
LITE_TTL_SECONDS = 300


class PersonaVersion(Data):
    """「她是谁」身份正文的自然语言全文快照（一版）.

    自然键 ``(lane, persona_id)``：泳道隔离 + 每个角色一条链。``narrative`` 是整篇
    重写的身份正文全文——与 ``bot_persona.persona_core`` 同族口吻，注入方零适配。
    ``source`` 是这一版从哪来（seed / review / owner，见模块 docstring）。
    ``written_at`` 是写下这版的现实时刻（**CST ISO8601 字符串，不是 datetime**）：
    命名避开框架保留列 ``created_at``（那是框架的落库时刻，语义不同，同 LivingDayPage 教
    训），类型也不改——这张表 prod 上已经有几十版真实数据，migrator 是 additive-only，
    改列类型 / 删列直接 ``MigrationError``、整批迁移回滚、pod crash loop。
    ``version`` 让多版正文 append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    narrative: str    # 身份正文全文（整篇重写的自然语言）
    source: str       # 这版从哪来：seed（出厂灌入）/ review（自动慢漂）/ owner（bezhai 干预）
    written_at: str   # 写下这版的现实时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def read_latest_version(*, lane: str, persona_id: str) -> PersonaVersion | None:
    """读 a：最新一版身份正文，**不分来源**——owner 盖版即生效；没有返回 None
    （冷启：读侧 fallback ``bot_persona`` 主表）。

    "最新"按 ``version`` 算，不按 ``written_at``：后者是调用方给的字符串，owner 人工
    盖版可以填任何时刻，按它排序的话一次填错时刻就让链永远停在那一版上。
    """
    return await select_latest(
        PersonaVersion, {"lane": lane, "persona_id": persona_id}
    )


async def read_latest_review_written_at(
    *, lane: str, persona_id: str
) -> str | None:
    """读 c：最新一条 **source='review'** 版本的 written_at；没有返回 None。

    review 的证据游标——下一班只消化这个时点之后写下的页。owner / seed 版本
    绝不入选：bezhai 人工盖版不能把证据窗口推走。首跑（链上还没有 review 版）
    返回 None = 窗口取全部现存页。
    """
    sql = (
        f"SELECT written_at FROM {table_name(PersonaVersion)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"AND source = 'review' ORDER BY version DESC LIMIT 1"
    )
    return await fetch_value(sql, lane=lane, persona_id=persona_id)


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


class PersonaChain:
    """persona 服务（:class:`app.living.services.Persona`）：「她是谁」的全部读写都经这里。

    唯一的状态是读书那份人设的进程内缓存（:meth:`lite_vars`）：一个进程一个对象，persona 插件
    在 setup 里建它。
    """

    def __init__(self) -> None:
        self._lite: dict[str, tuple[dict[str, str], float]] = {}

    async def prompt_vars(self, *, lane: str, persona_id: str) -> dict[str, str]:
        """把「她是谁」摆成 prompt 变量 —— **恰好两个**，全仓一处组装。

        **只有不随轮次变的东西才配当 prompt 变量**：她叫什么、她是个什么样的人。每一轮
        都变的（快照、手机信封、那一天的材料）一律走 USER 消息，理由是 prompt 变量没有
        编译期校验、改名会静默渲染成字面量，能少一个就少一个。同理这两个键名
        （``persona_name`` / ``persona_core``）就是 Langfuse 上那两个 prompt 正文里写着
        的名字，改代码这边等于让它们原样渲染成字面量出现在她眼前。

        正文取链上最新一版（不分来源，owner 盖版即刻生效），链空 / 那一版空白才退回主表
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
        """她一轮 prompt 里「她是谁」那一段的两个变量，一轮开始取齐 prompt 时算一次
        （:meth:`prompt_vars`，这一轮的泳道和她）。"""
        return await self.prompt_vars(lane=round.lane, persona_id=round.participant)

    async def review_vars(self, *, persona_id: str) -> dict[str, str]:
        """每周回看那一轮的两个 prompt 变量：她叫什么，和**她本来是谁**（外部锚）。

        ``persona_core`` 装的是 ``bot_persona.persona_core`` **原始那一列的原文**，不是
        链上最新一版。这跟 :meth:`prompt_vars` 里那个同名变量的值不同，是刻意的：两者答的是
        「她本来是谁」和「她现在是谁」两个不同的问题。回看那一轮要在自己上一版的基础上改自己，
        参照物必须站在这条循环外面，不然积累的是回声。

        **锚拿不到就 fail fast**（``bot_persona`` 没这行、或者那一列是空白）：那一轮的全
        部意义就是"在锚的约束下改自己"，没有锚还照跑的话，她会在一段空白的参照下重写自
        己，而这件事在库里、在日志里、在她的表现上都看不出来——只有几个月后那份「我是谁」
        已经不是她了。抛出去由她回看队列上的钟记一条 warning，这个人这一周的班不跑，另外两个
        照跑。
        """
        persona = await find_persona(persona_id)
        core = getattr(persona, "persona_core", None) or ""
        if not core.strip():
            raise ValueError(
                f"persona_review_vars: bot_persona.persona_core is blank for "
                f"persona_id={persona_id!r} — 这一轮没有外部锚可用，不跑"
            )
        return {
            "persona_name": getattr(persona, "display_name", "") or persona_id,
            "persona_core": core,
        }

    async def lite_vars(self, *, persona_id: str) -> dict[str, str]:
        """读书 agent 的两个 prompt 变量 ``{persona_name, persona_lite}``，进程内缓存
        :data:`LITE_TTL_SECONDS`。

        ``persona_lite`` 优先取版本链最新一版（不分来源——owner 盖版即生效），链空 / 链最新版
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
        写侧已拦空白落版，这里是防御纵深（owner 人工插空等其它写入口）。
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

    async def latest(self, *, lane: str, persona_id: str) -> PersonaText | None:
        """链上最新一版（:func:`read_latest_version`，不分来源）；链空返回 ``None``。"""
        row = await read_latest_version(lane=lane, persona_id=persona_id)
        if row is None:
            return None
        return PersonaText(
            narrative=row.narrative,
            source=row.source,
            written_at=row.written_at,
            version=row.version,
        )

    async def has_review_this_week(
        self, *, lane: str, persona_id: str, now: datetime | None = None
    ) -> bool:
        """读 b：本周（周一 00:00 CST 起）是否已有 **source='review'** 的版本。

        自动班的幂等口径：True = 本周班已完成、今天不跑。只认 review——同周的
        owner / seed 版本在场仍返回 False（bezhai 盖版不挡自动班）。
        written_at 解析失败（理论上不会：全部由 now_cst_iso 写出）按"不在本周"算
        ——宁可多跑一班，不能让脏数据把慢漂永远卡死（fail-open 方向一致）。
        """
        latest = await read_latest_review_written_at(
            lane=lane, persona_id=persona_id
        )
        if latest is None:
            return False
        written = parse_time(latest)
        if written is None:
            return False
        return written >= week_start_cst(now if now is not None else now_cst())

    async def seed_chain(self, *, lane: str, persona_id: str) -> bool:
        """v0 灌入：链为空时把 ``bot_persona.persona_core`` 原文落为第一版
        （source='seed'）；链非空零操作。返回是否真的写入了。

        **灌的必须是"她在有自己的版本之前实际读到的那份"**，而那份就是
        ``persona_core``——:meth:`prompt_vars` 在链空时退回的正是这一列。灌
        ``persona_lite`` 的话链上的历史是断的：v1 记着一段她从没读过的东西，而下一版
        是在她真正读到的那份上改出来的，中间那一跳在库里看不出任何异常。

        幂等靠 ``insert_append`` 的 CAS（``expected_current_ver=0``：只有链上
        MAX(version)=0 即一版都没有时才插入），检查和写入是同一条原子语句——
        重跑无害、并发双跑也只落一行。``bot_persona`` 没这行 = 没有原文可灌，
        fail fast 不静默写空版。
        """
        persona = await find_persona(persona_id)
        if persona is None:
            raise ValueError(
                f"seed_persona_chain: bot_persona has no row for "
                f"persona_id={persona_id!r} — nothing to seed from"
            )
        inserted = await insert_append(
            PersonaVersion(
                lane=lane,
                persona_id=persona_id,
                narrative=persona.persona_core,
                source="seed",
                written_at=now_cst_iso(),
            ),
            expected_current_ver=0,
        )
        return inserted == 1

    async def write_version(
        self, *, lane: str, persona_id: str, narrative: str, source: str, written_at: str
    ) -> None:
        """append 一版身份正文（带来源）。

        durable 语义同写日记：append 新版本、无 dedup。整次 review 失败重跑
        可能再 append 一次语义相同的版本——无害，读侧只认最新版，版本链留痕。
        """
        await insert_append(
            PersonaVersion(
                lane=lane,
                persona_id=persona_id,
                narrative=narrative,
                source=source,
                written_at=written_at,
            )
        )
