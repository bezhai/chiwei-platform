"""NPC 名册 —— 世界里有名有姓的固定人物（NPC 层第一刀）.

赤尾世界里，除了三姐妹（赤尾 / 千凪 / 绫奈）和用户（哥哥），原本没有别的真实存在
的人——绫奈嘴里的同桌、千凪的同事，只活在她们聊天那一刻的叙述里。这张表给世界补
上一批有名有姓的固定 NPC（7 个种子：绫奈 3 / 赤尾 2 / 千凪 2），让 world 推演时知道
她们存在。NPC **不是** persona / 不是 agent：不进 ``bot_persona``、不给工具循环，
只是 world 推演时引用的人物设定底料。

照 WorldArc / RelationshipPage / PersonaVersion 模板（第 N 次复用）：Key + Version、
append-only、读最新一版、整篇重写——演化层（下一刀）更新某个 NPC 直接 append 那一行
的新版，读侧只认最新版。**逐 NPC 一行**是「一组各自独立的人」这类数据的自然建模
（跟关系页一个用户一行同理），改单个 NPC 直接 append 那一行——这正是下一刀演化层要的。

钉死的语义：

  * 自然键 ``(lane, npc_name)``：泳道隔离（coe / ppe 绝不能覆盖 prod 的名册）+
    每个 NPC 一条链。``npc_name`` 是 NPC 的稳定标识（名字），跟关系页 other_user_id
    的 ``npc:xxx`` 约定对齐（event / 关系页是后面的刀，本刀只落名字本身）。
  * ``relates_to`` 是该 NPC 主要关联哪个姐妹的 persona_id（akao / chinagi / ayana）——
    world 据此把名册归到对应姐妹。
  * ``sketch`` 是性格底色 + 会冒什么事的自然语言速写（自然语言、不拆结构化字段，
    与 sibling 的 narrative 同族口吻）。
  * ``version`` 让同一 NPC 的多版速写 append-only 保留历史、读最新一版（演化层用）。
    命名上 ``npc_name`` / ``relates_to`` / ``sketch`` / ``version`` 都不撞 migrator
    保留列（id / created_at / updated_at / dedup_hash），无需另起时刻字段（本刀不记
    NPC 的写入时刻——它不像页 / 阶段那样要按时间游标读，演化层若需要再加）。

写入走 framework 的 ``insert_append``（Version 自增），读最新走 ``select_latest``；
list 一个 lane 全部 NPC 是 framework 没提供的只读查询，照 acts.py / day_page_exists /
list_relationship_pages 的先例在 framework 持久化写好的真实表上直接 SELECT
（DISTINCT ON 每人取最新一版）——不绕开 framework 持久化原语。

seed：7 个种子 NPC 一次性灌库，逐 NPC 幂等（:func:`seed_npc_roster`，CAS
expected_current_ver=0，照 :func:`app.life.persona_chain.seed_persona_chain` 的
先例）——重跑无害、并发双跑也只落一行、链非空（已被演化层动过）的 NPC 绝不被出厂
速写盖回。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, Key, Version
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest


class NPCRoster(Data):
    """世界里一个有名有姓的固定 NPC 的设定速写（一版）.

    自然键 ``(lane, npc_name)``：泳道隔离 + 每个 NPC 一条链。``relates_to`` 是该
    NPC 主要关联哪个姐妹的 persona_id（akao / chinagi / ayana），world 据此归类。
    ``sketch`` 是性格底色 + 会冒什么事的自然语言速写（整篇重写、不拆结构化字段）。
    ``version`` 让同一 NPC 的多版速写 append-only 保留历史、读最新一版（演化层用）。
    """

    lane: Annotated[str, Key]
    npc_name: Annotated[str, Key]   # NPC 的稳定标识（名字，对齐关系页 npc:xxx 约定）
    relates_to: str                 # 主要关联的姐妹 persona_id（akao / chinagi / ayana）
    sketch: str                     # 性格底色 + 会冒什么事的自然语言速写
    version: Annotated[int, Version] = 0


@dataclass(frozen=True)
class NPCSeed:
    """一个种子 NPC 的骨架（名字 + 关联姐妹 + 速写）——seed 灌库用。"""

    npc_name: str
    relates_to: str
    sketch: str


# 7 个种子 NPC（绫奈 3 / 赤尾 2 / 千凪 2），Claude 编骨架（spec 决策 5，用户已授权）。
# 演化交给 world + 后续演化层；路人 world 即兴长不建档、不进这张表（spec non-goals）。
NPC_SEEDS: tuple[NPCSeed, ...] = (
    # 绫奈（14，初中生）——
    NPCSeed(
        npc_name="林小满",
        relates_to="ayana",
        sketch=(
            "同桌兼死党，同岁。比绫奈稳、像小大人，会照顾爱慌的她，自己也藏着小心事。"
            "会冒：约周末、考前互打气、闹小别扭又和好。"
        ),
    ),
    NPCSeed(
        npc_name="顾舟",
        relates_to="ayana",
        sketch=(
            "班长，认真严肃，总“盯着”爱走神画画的绫奈，其实是另一种照顾。"
            "会冒：催交作业、收作业较劲、小组活动分一起。"
        ),
    ),
    NPCSeed(
        npc_name="沈乐",
        relates_to="ayana",
        sketch=(
            "同班开心果 / 小活宝，爱起哄带头闹。"
            "会冒：课间拉她胡闹、起哄她糗事、约人放学买奶茶。"
        ),
    ),
    # 赤尾（18，刚高考完）——
    NPCSeed(
        npc_name="陈鹿",
        relates_to="akao",
        sketch=(
            "高中闺蜜，一起熬过高三，毒舌互怼但最懂彼此。"
            "会冒：约出去浪、互相打击式关心、一起盯分数线焦虑暑假。"
        ),
    ),
    NPCSeed(
        npc_name="池夏",
        relates_to="akao",
        sketch=(
            "高中死党（女生），大大咧咧、组局王。"
            "会冒：撺掇赤尾出去浪 / 开黑、转沙雕东西、考完一起疯。"
        ),
    ),
    # 千凪（24，上班）——
    NPCSeed(
        npc_name="许念",
        relates_to="chinagi",
        sketch=(
            "同部门同事兼朋友，职场互相照应。"
            "会冒：下班约饭、吐槽工作、周末小聚、找千凪这个“靠谱的人”倒苦水。"
        ),
    ),
    NPCSeed(
        npc_name="苏晴",
        relates_to="chinagi",
        sketch=(
            "大学室友兼多年闺蜜，各自工作但常联系。是千凪能卸下“大姐”担子放松的人。"
            "会冒：深夜聊心事、约周末见面。"
        ),
    ),
)


async def write_npc(
    *, lane: str, npc_name: str, relates_to: str, sketch: str
) -> None:
    """append 一版 NPC 速写（演化层对这个 NPC 整篇重写）。

    durable 语义同 write_world_arc / write_persona_version：append 新版本、无 dedup。
    重跑可能再 append 一次语义相同的版本——无害，读侧只认最新版，版本链留痕。
    """
    await insert_append(
        NPCRoster(
            lane=lane,
            npc_name=npc_name,
            relates_to=relates_to,
            sketch=sketch,
        )
    )


async def read_npc(*, lane: str, npc_name: str) -> NPCRoster | None:
    """读某泳道某 NPC 最新一版速写，没有返回 None（名册里没这个人）。"""
    return await select_latest(NPCRoster, {"lane": lane, "npc_name": npc_name})


async def list_npc_roster(*, lane: str) -> list[NPCRoster]:
    """list 某泳道全部 NPC（每个 NPC 只取最新一版），空名册返回空表。

    world 当天首轮把整份名册 list 出来、按 relates_to 归到对应姐妹、拼成「世界的
    固定人物」一段纳入推演（engine 调用方）。照 list_relationship_pages 的先例在
    framework 持久化写好的真实表上做只读 SELECT（DISTINCT ON 每人取最新一版）；
    写入仍走 ``insert_append``，不绕开 framework 持久化原语。
    """
    sql = (
        f"SELECT DISTINCT ON (npc_name) * "
        f"FROM {_table_name(NPCRoster)} "
        f"WHERE lane = :lane "
        f"ORDER BY npc_name ASC, version DESC"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), {"lane": lane})
        return [
            NPCRoster(**{k: row[k] for k in NPCRoster.model_fields})
            for row in r.mappings()
        ]


async def seed_npc_roster(*, lane: str) -> int:
    """把 7 个种子 NPC 灌进指定 lane，逐 NPC 幂等。返回真正写入的条数。

    每个 NPC 靠 ``insert_append`` 的 CAS（``expected_current_ver=0``：只有该 NPC
    链上 MAX(version)=0 即一版都没有时才插入），检查和写入是同一条原子语句——重跑
    无害、并发双跑也只落一行、链非空（已被演化层动过）的 NPC 绝不被出厂速写盖回。
    照 :func:`app.life.persona_chain.seed_persona_chain` 的先例。
    """
    inserted = 0
    for seed in NPC_SEEDS:
        n = await insert_append(
            NPCRoster(
                lane=lane,
                npc_name=seed.npc_name,
                relates_to=seed.relates_to,
                sketch=seed.sketch,
            ),
            expected_current_ver=0,
        )
        inserted += n
    return inserted
