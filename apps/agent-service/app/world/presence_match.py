"""在场匹配 — Task 3：事件投递 = 客观作用域 + 在场匹配.

旧范式里 world 主观挑收件人（``notify(recipients, ...)``）——它一边推演世界、一边
替每条动静决定「这条该推给谁」。这两件事混在一起：world 既是客观世界的推演者、又
成了收件人的主观裁决者。新范式把它们切开——world 只标这条动静的**客观作用域**
（发生在哪、是广播性的还是指向某个具体的人），「谁收到」交给这道独立的**在场匹配**：

  拿事件作用域，对一下每个角色此刻客观在哪（从 life 的 ``current_state`` 自然语言
  读），谁在场谁就收到。「下课铃响，全校广播」→ 在学校的角色都收到；「屋里有人喊
  某人吃饭」→ 在家的那个人收到。

它只看「事件发生在哪 + 谁在那」，**绝不看「谁该不该被叫醒」**——这是它与旧范式
（world 看角色活跃度判该叫谁、life 一静止就没人叫、越静越不叫）不自锁的根本。喂给
它的输入里没有任何唤醒 / 活跃度 / 状态新旧语义。

赤尾设计宪法（硬约束）：

  * 在场判定**必须是模型判断**——这里整段就是一次离线 LLM 调用（:func:`match_present_
    personas`）。代码侧绝无任何确定性匹配规则：没有距离阈值、没有位置枚举状态机、
    没有 if/else 判在场、没有查表、没有关键词匹配。模糊在场（下雨她在室内还是室外、
    看没看见窗外）留给模型判断——不确定性是 feature，不加规则去消除它。
  * 这道判断接 langfuse trace（同 world 续写 / 反思 / 眼睛：``AgentContext.session_id``
    做归组标签）、用 offline-model 档位（异步内部判断，不用主对话模型）。

唯一的非模型环节是两处机制兜底，都不替模型判在场：

  * **无候选短路**：没有任何角色位置（谁都还没活过一轮）时直接返空、免调一次 LLM
    （本就无人可匹配，不是用规则替模型判）。
  * **候选过滤**：模型自然语言判断偶发返一个候选外的 id（幻觉），投递只落真候选里
    的人、丢弃候选外的 id（投递正确性兜底，不是替模型判在场）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role

logger = logging.getLogger(__name__)

# 在场匹配的独立 AgentConfig：prompt id 钉为 "world_presence_match"（langfuse 上
# 新建、世界底色与 world_deliberate 同族）。offline-model 档位——异步内部判断，不用
# 主对话模型（同反思 / 眼睛）。recursion_limit 用默认值：这是一次结构化判定（extract），
# 不跑工具循环。
_PRESENCE_CFG = AgentConfig(
    "world_presence_match", "offline-model", "world-presence-match"
)


class PresenceVerdict(BaseModel):
    """在场匹配的结构化判定：此刻够得着这条动静的角色 id 列表。

    用 function-calling / structured output 让模型直接给结构化结果（present 列表），
    不从自然语言里抠 id（赤尾 footgun：LLM 输出优先 function calling > JSON > 文本）。
    """

    present: list[str] = Field(
        default_factory=list,
        description="此刻客观在场、够得着这条动静的角色 id 列表（够不着的不要列）",
    )


def build_presence_messages(
    *, scope: str, persona_locations: dict[str, str]
) -> list[Message]:
    """拼在场匹配的单条 user 消息：事件作用域 + 每个角色此刻客观位置。

    **只喂这两样**（在场匹配只看「事件发生在哪 + 谁在那」）：作用域原文 + 每个角色
    的 ``current_state`` 原文。**绝不喂**任何唤醒 / 活跃度 / 状态新旧语义（next_wake_at /
    observed_at / 「多久没动」「该不该醒」）——喂了会把判断从「判在场」带偏成「判唤醒」、
    重新自锁。位置原文原样进 prompt（不在代码里预消化成结构化位置标签：那等于用代码
    规则替模型理解位置，违宪）。
    """
    location_lines = "\n".join(
        f"- {persona_id}：{location}"
        for persona_id, location in persona_locations.items()
    )
    user_content = (
        "你来判断一条客观动静此刻有谁够得着（在场匹配）。\n\n"
        "下面给你这条动静的**客观作用域**（它发生在哪、是全场都听 / 看得到的广播，"
        "还是冲着某个具体的人去的），以及**每个角色此刻客观在哪**。你只做一件事："
        "拿作用域对一下每个角色的位置，判断谁此刻够得着这条动静——在那个空间 / 范围"
        "里的人够得着，不在的够不着。\n"
        "你只看「这条动静发生在哪 + 谁在那」，不去想别的（不判断谁该不该被打扰、"
        "谁在干嘛重不重要——那些都不是你的事）。够得着就列进 present，够不着就不列。\n"
        "**位置信息不足 / 拿不准时，倾向把她判进 present（保活优先：宁可多投一个，"
        "也别让世界因为信息不足就起不来）。** 比如她此刻在哪还说不清楚（位置是空的、"
        "或只写着「还不知道她此刻在哪」），又或者动静在室外、而她在室内说不清看没看见——"
        "这些拿不准的，默认偏向算她在场。只有当作用域明确指向某个她**显然不在**的地方"
        "（她明明在公司、动静却只在家里那间屋里），才把她排除。\n\n"
        f"【这条动静的客观作用域】\n{scope}\n\n"
        f"【每个角色此刻客观在哪】\n{location_lines}\n\n"
        "把此刻够得着这条动静的角色 id 都列进 present（够不着的别列）。"
    )
    return [Message(role=Role.USER, content=user_content)]


async def match_present_personas(
    *,
    scope: str,
    persona_locations: dict[str, str],
    trace_session_id: str | None = None,
) -> list[str]:
    """对一条动静的客观作用域做在场匹配 → 返回此刻够得着它的角色 id 列表（纯模型判断）。

    ``scope`` 是 world 标的客观作用域（自然语言：发生在哪、广播还是指向谁）；
    ``persona_locations`` 是每个角色此刻的客观位置（``current_state`` 原文，调用方从
    life 状态读好传进来）。一次离线 LLM 结构化判定（:class:`PresenceVerdict`）给出
    在场列表——代码侧无任何确定性匹配规则（赤尾宪法）。

    两处机制兜底（都不替模型判在场）：

      * ``persona_locations`` 为空（谁都还没活过一轮）→ 直接返空、免调 LLM（无候选
        本就无人可匹配）。
      * 模型返了候选外的 id（幻觉）→ 过滤掉、只留真候选里的人（投递正确性兜底）。

    trace：``trace_session_id`` 塞进 ``AgentContext.session_id`` 做 langfuse 归组标签
    （同 world 续写 / 反思 / 眼睛），让这道判断的 trace 归到 world 当天那条 session。
    ``max_retries`` 用 extract 默认（只读判定、无 durable 副作用，重放无害）。
    """
    if not persona_locations:
        return []

    messages = build_presence_messages(
        scope=scope, persona_locations=persona_locations
    )
    # session_id 塞进 extract 做 langfuse 归组标签（同 world 续写 / 反思 / 眼睛）：
    # 这道判断的 trace 归到 world 当天那条 session。extract 仍是无状态结构化判定
    # （不读写 transcript）——session_id 只是 trace 标签、不是续接。lane / round_id
    # 在场匹配本身不需要（没有 durable 副作用、不需要幂等 id、不需要泳道分区写）：
    # 它是一道纯只读判定，输入只有作用域 + 各角色位置。
    verdict = await Agent(_PRESENCE_CFG).extract(
        PresenceVerdict, messages, session_id=trace_session_id
    )
    assert isinstance(verdict, PresenceVerdict)

    # 候选过滤（投递正确性兜底）：只留真候选里的人，丢弃模型幻觉出的候选外 id。
    # 按候选的输入顺序输出（稳定、可读），不按模型返回顺序。
    present_set = set(verdict.present)
    return [pid for pid in persona_locations if pid in present_set]
