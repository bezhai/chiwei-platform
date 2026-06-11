"""世界阶段透传给角色 —— 「你们一家所处的现实阶段」段的单一渲染处.

事故根因：世界阶段（``WorldArc``，「跨周月仍然成立的世界进展」）只活在 world 引擎的
推演输入里，角色（life agent / chat）不读它——世界明明已翻页（比如某段人生阶段结束），
她的 persona 出厂设定还停在旧页、记忆链又可能被清过，于是她照旧设定过日子、穿帮。
机制透传：把最新一版世界阶段渲染成一段给**活在里面的人**看的第一人称段落，life 每轮
唤醒的 stimulus 与 chat 的 inner_context 都喂这一段。

透传不破坏信息差：世界阶段的写作纪律本来就只写「在场所有人都知道的公共进展」（禁
场景 / 私密），全 persona 同享同一份，角色读到的是"我本来就知道的事"。这与 life 绝不
读 ``WorldState`` 全局快照（谁此刻在哪的全局真相）是两回事——那条命门不动。

视角区别：world 引擎侧的 ``_arc_section`` 是给**推演者**看的（空白时引导它顺着底色
推演）；这里是给**角色**看的（空链 / 读失败时整段不渲染、绝不塞占位文案——没有的事
不该出现在她的意识流里）。

框架文案是机制层，**绝不硬编任何剧情事实**（高考 / 角色名 / 日期都不准出现——宪法，
有测试钉死）。
"""

from __future__ import annotations

import logging

from app.world.arc import read_world_arc  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# 平直的第一人称框架标头：告诉她"这些是你本来就知道、亲历着的公共进展"。
_ARC_AWARENESS_HEADER = (
    "【你们一家所处的现实阶段】"
    "下面是这个家此刻真实走到的人生阶段——这些事你都知道、亲历着："
)


async def render_arc_awareness(*, lane: str) -> str:
    """读某泳道最新一版世界阶段，渲染成给角色看的第一人称段落。

    lane 由调用方按各自既有口径给（life 用唤醒事件的 lane，chat 用
    ``current_deployment_lane() or "prod"``），这里不发明新口径。

    空链（还没人写过世界阶段）/ narrative 全空白 / 读失败 → 返回 ``""``，调用方整段
    不渲染。读失败吞掉只 log：透传是上下文增强，绝不能塌掉 chat（inner_context 不能塌）
    或杀掉一轮 life 思考。
    """
    try:
        arc = await read_world_arc(lane=lane)
    except Exception as e:
        logger.warning("[arc_awareness] failed to read world arc (lane=%s): %s", lane, e)
        return ""

    if arc is None:
        return ""
    narrative = arc.narrative.strip()
    if not narrative:
        return ""
    return f"{_ARC_AWARENESS_HEADER}\n{narrative}"
