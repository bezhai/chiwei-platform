"""persona review 的写入工具 — 身份正文一件（``PERSONA_REVIEW_TOOLS``）.

周级 persona review 跑一个无会话 Agent（她的传记作者，回读这段日子的日页 /
关系页 / 世界阶段后轻轻重写「她是谁」），手里只有这一件工具：

  * :func:`update_persona` —— 整篇重写她的身份正文，落 persona 版本链一版
    （source='review'，:func:`app.life.persona_chain.write_persona_version`）。

契约照睡前回顾工具（update_day_page / update_relationship_page，WorldArc 范式
工具面的又一次复用）：

  * **签名只留语义参数**（narrative）——lane / persona 是机制层的事，从 ambient
    :class:`~app.agent.context.AgentContext` 的 ``features`` 读（key 见下方常量），
    不放进签名让模型填。
  * **来源钉死 'review'**：这件工具是自动慢漂的唯一写入方——seed（出厂灌入）走
    :func:`~app.life.persona_chain.seed_persona_chain`，owner（bezhai 干预）走
    人工 mutation，绝不经这只手。
  * **时间自填**：``written_at`` 由工具体填现实当前 CST（客观时间不让模型编）。
    它同时是下一班的证据游标——必须是真实落版时刻。
  * **故意不包 @tool_error**：这是 review 环节的 durable 写。写库失败若被包成
    tool result 字符串喂回模型，Agent.run 会正常返回 → 核验复查发现本周没有
    review 版、当班作失败处理还好；更糟的是模型可能重试出多版。让异常照实穿透
    炸掉整次 review（``Tool.invoke`` 的设计语义：未包 @tool_error 即传播），由
    review 的 fail-open 接住：本周版不落、下一班自动补。durable mutation 失败
    要可见。
  * **工具集物理隔离**：``PERSONA_REVIEW_TOOLS`` 只有这一件，与 life 活轮 /
    睡前回顾的工具互不相通——慢钟无手碰快钟。

ambient features key 约定（review 本体构造 AgentContext 时两个都要塞齐）：
``FEATURE_PERSONA_REVIEW_LANE`` / ``FEATURE_PERSONA_REVIEW_PERSONA``。缺绑定时
:func:`_require_feature` 抛 LookupError 失败快——空 lane / 空 persona 落库会写出
永远读不回来的脏 Key，比炸掉整次 review 更糟。
"""

from __future__ import annotations

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.infra import cst_time
from app.life.persona_chain import write_persona_version

# review 本体往 AgentContext.features 塞的两个机制绑定 key（不散落字符串、从这里
# import）。命名带 persona_review 前缀，与 life_review_* / world_* 不同命名空间
# ——几把钟的 ambient 绑定绝不互相误读。
FEATURE_PERSONA_REVIEW_LANE = "persona_review_lane"
FEATURE_PERSONA_REVIEW_PERSONA = "persona_review_persona_id"


def _require_feature(key: str) -> str:
    """从 ambient context features 读一个机制绑定，缺了就失败快。

    没绑 context 时 ``get_context()`` 本身抛 LookupError；绑了但 features 没塞
    齐（review 本体的 wiring bug）也抛 LookupError——绝不拿空字符串当 Key 落库。
    """
    value = get_context().features.get(key, "")
    if not value:
        raise LookupError(
            f"ambient context features 缺少 {key!r}——persona review 本体构造 "
            "AgentContext 时必须塞齐 lane / persona 两个绑定"
        )
    return value


@tool
async def update_persona(narrative: str) -> str:
    """整篇重写她的身份正文（「她是谁」），新的一版取代旧版。

    把这段日子真实留在她身上的痕迹，轻轻写进她的身份正文：绝大部分原文原样
    保留，一次只让真实经历留下一两笔痕迹；每一处改动都要能从证据里指出出处，
    证据里没有的事一个字都不写；她的底色不让渡——经历改变的是人生阶段、关系
    厚度、新的在意，不是她性格的内核。口吻与格式与原文同族，改完读起来仍是
    同一篇文章。

    每次调用都是**整篇重写**：新的一版**取代**旧版，不是往后追加。写下的时刻
    由系统自动记（你不用、也不能填时间）。

    Args:
        narrative: 重写后的身份正文全文（整篇交回的自然语言）。

    Returns:
        一句确认文本。
    """
    if not narrative or not narrative.strip():
        # 机制安全阀（不是内容检测器）：空白正文落成 review 版 = 五个读取方注入
        # 空 identity，且本周幂等已满足、不会自动补。抛 ValueError 穿透炸轮（不包
        # @tool_error）→ review 的 fail-open 接住：空产出按失败算、下一班重试
        # （同 sediment 空产出先例）。
        raise ValueError(
            "update_persona got a blank narrative; refuse to land an empty "
            "review version"
        )
    lane = _require_feature(FEATURE_PERSONA_REVIEW_LANE)
    persona_id = _require_feature(FEATURE_PERSONA_REVIEW_PERSONA)
    # written_at 跟现实走，由代码填现实当前 CST（客观时间不让模型编）。它同时
    # 是下一班的证据游标（read_latest_review_written_at 只认 review 版）。
    written_at = cst_time.now_cst_iso()
    await write_persona_version(
        lane=lane,
        persona_id=persona_id,
        narrative=narrative,
        source="review",
        written_at=written_at,
    )
    return "已写下新一版身份正文"


# persona review 工具集（一件）：身份正文。与 life 活轮 / 睡前回顾的工具物理
# 隔离——慢钟是另一双手，不碰快钟的手。
PERSONA_REVIEW_TOOLS = [update_persona]
