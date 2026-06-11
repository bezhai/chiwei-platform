"""Transcript 折叠 — 当天意识流到阈值整卷压成「她自己的沉淀」（沉淀 Task 1，纯逻辑层）.

按天滚动的 world/life 会话 transcript 每轮全量重发，现有保护只有 200 条 / 256KB
硬截断——较早的经历直接蒸发（失忆），且每次掐头都重写消息前缀、逐轮 bust prompt
cache。本模块把「截断」换成「沉淀」（spec 决策 2/4）：transcript 达到
:data:`FOLD_TRIGGER_MESSAGES` 时整卷压成**单条合成 USER 消息**——

  * 上半 = **沉淀正文**：她自己口吻的当天回忆，由可注入的异步回调产生（真正的
    沉淀 agent 是 Task 2 的事，这里只定回调契约）；
  * 尾部 = **机制载荷段**：被折叠各轮的完整 round marker 字符串逐行保全——
    life turn 幂等靠在 USER 消息里扫 marker 子串、world 游标补推靠从 marker 解析
    终点，两套现有机制对折叠后的 transcript 零改动继续命中（spec 决策 3）。

折叠后 transcript 只剩这一条，新轮继续 append；再次达阈值时重折叠：旧沉淀 +
新轮交回调**整篇重写**，marker 由**代码做并集**、绝不经 LLM（铁律①）。

机制载荷三条铁律（spec 决策 3，codex T1 必改）：
  ① 重折叠时 marker 由代码并集合并，绝不交给沉淀回调改写；
  ② marker 字符串不得混进沉淀正文——:func:`build_fold_message` 净化 + warning；
  ③ 睡前回顾证据拼装过滤掉机制载荷（见 ``app.life.review``，用本模块的
     :func:`split_fold_message` / :func:`strip_round_markers`）。

两阶段解耦（spec 决策 5）：折叠**不**内联在轮写回里——``append_session`` 照常
（与现状字节级一致），:func:`fold_session` 是其后的独立步骤，由调用方在同一
串行窗口内显式调用（调用点 Task 2 接线）。策略 ``None`` = 完全不折叠（chat 等
不带策略的调用方零感知）。回调异常 / 超时 = 本版不折、原样不动（fail-open），
200 条 / 256KB 硬截断保留作最后兜底。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from app.agent.neutral import Message, Role
from app.agent.session import load_session_versioned, replace_session

logger = logging.getLogger(__name__)

# 折叠触发阈值（spec 决策 4，bezhai 拍板）：transcript 达到 100 条整卷压一条。
# 到 200 条硬上限（TRANSCRIPT_MAX_MESSAGES）余量 = 整整一倍触发间距——正常路径
# 永远到不了硬截断，硬截断只剩「折叠一直失败」时的最后兜底。
FOLD_TRIGGER_MESSAGES = 100

# 折叠消息的两个段标记（固定行级常量，机读用）：首行 FOLD_HEADER 标识这条消息
# 是折叠产物；最后一个 FOLD_MARKERS_HEADER 行之后是机制载荷段（每行一条完整
# round marker）。两行之间是沉淀正文。Task 2 的沉淀 agent 与所有读取方都按这
# 两个常量识别，不得另造。
FOLD_HEADER = "[transcript-fold]"
FOLD_MARKERS_HEADER = "[fold-markers]"

# round marker 的结构语法：life ``[life-round:{rid}]`` 与 world
# ``[world-round:{rid}|end:...]`` 共享 ``[<kind>-round:...]`` 形态。这里只认
# 这个**结构**、不复刻两边的具体格式（具体格式的权威仍在 life_wake / world
# engine 各自的 _ROUND_MARKER_PREFIX）；新增不符合此形态的 marker 种类时折叠
# 会漏保全——由 tests/unit/agent/test_session_fold.py 直接调两边扫描函数钉死。
_ROUND_MARKER_RE = re.compile(r"\[[a-z]+-round:[^\]]*\]")

# 沉淀回调契约（Task 2 的沉淀 agent 按它接）：
#   入参 = (旧沉淀正文 | None 首折, 本次被折叠的轮消息序列)
#   返回 = 整篇重写后的沉淀正文（她口吻的自然语言段，**不含任何 marker**——
#          marker 由代码并集，混进来也会被 build_fold_message 净化掉）
# 异常 / 超时（回调自己包 wait_for 后抛 TimeoutError）= fail-open，本版不折。
SedimentWriter = Callable[[str | None, list[Message]], Awaitable[str]]


@dataclass(slots=True)
class FoldPolicy:
    """折叠策略——由调用方显式注入，写回路径默认没有它（不折叠）。"""

    write_sediment: SedimentWriter
    trigger_messages: int = FOLD_TRIGGER_MESSAGES


def is_fold_message(message: Message) -> bool:
    """这条消息是不是折叠产物：USER role 且首行恰为 :data:`FOLD_HEADER`。

    role 必须收口在 USER——life / world 两套幂等扫描都只看 USER 消息，折叠产物
    不是 USER 时载荷段里的 marker 就够不着了。
    """
    if message.role != Role.USER:
        return False
    return message.text().split("\n", 1)[0] == FOLD_HEADER


def split_fold_message(message: Message) -> tuple[str, list[str]]:
    """把折叠消息拆回（沉淀正文, marker 列表）。非折叠消息抛 ``ValueError``。

    按**最后一个** :data:`FOLD_MARKERS_HEADER` 行切分——build 时已净化沉淀正文、
    其中不会再有这个标记行，取最后一个是防御（marker 行自身不含换行，不会伪造
    出更晚的标记行）。
    """
    if not is_fold_message(message):
        raise ValueError("not a fold message")
    lines = message.text().split("\n")
    try:
        sep_at = len(lines) - 1 - lines[::-1].index(FOLD_MARKERS_HEADER)
    except ValueError as exc:
        raise ValueError("fold message has no markers section") from exc
    sediment = "\n".join(lines[1:sep_at])
    markers = [line for line in lines[sep_at + 1 :] if line.strip()]
    return sediment, markers


def extract_round_markers(messages: Iterable[Message]) -> list[str]:
    """从轮消息序列里提取完整 round marker 字符串（保序、去重）。

    只扫 USER 消息——与 life / world 两套幂等扫描同一口径（marker 印在 USER
    stimulus 里；ASSISTANT 复述出的 marker 从来不参与幂等判定，折叠也不保全它）。
    """
    seen: list[str] = []
    for m in messages:
        if m.role != Role.USER:
            continue
        for match in _ROUND_MARKER_RE.finditer(m.text()):
            marker = match.group(0)
            if marker not in seen:
                seen.append(marker)
    return seen


def strip_round_markers(text: str) -> str:
    """把文本里的 round marker 摘干净（回顾证据过滤用，铁律③）。

    marker 独占一行 → 整行删（不留空壳行）；混在行内 → 只摘 marker 子串、其余
    原样保留。没有 marker 的行逐字节不动。
    """
    kept: list[str] = []
    for line in text.split("\n"):
        if _ROUND_MARKER_RE.search(line):
            line = _ROUND_MARKER_RE.sub("", line)
            if not line.strip():
                continue
        kept.append(line)
    return "\n".join(kept)


def build_fold_message(sediment: str, markers: list[str]) -> Message:
    """拼装折叠产物：单条合成 USER 消息 = 沉淀正文 + 机制载荷段。

    铁律②在这里落地：沉淀正文里若混进了 marker 字符串或段标记行（回调是 LLM、
    管不住嘴），净化掉 + warning（不静默）——否则载荷段的可靠切分和幂等扫描的
    「marker 只出现在载荷段」语义都会被污染。载荷 marker 保序去重。
    """
    clean_lines: list[str] = []
    sanitized = False
    for line in sediment.split("\n"):
        if line.strip() in (FOLD_HEADER, FOLD_MARKERS_HEADER):
            sanitized = True
            continue
        if _ROUND_MARKER_RE.search(line):
            sanitized = True
            line = _ROUND_MARKER_RE.sub("", line)
            if not line.strip():
                continue
        clean_lines.append(line)
    if sanitized:
        logger.warning(
            "fold sediment contained mechanism marker text; sanitized it out "
            "(markers must never be written by the sediment callback)"
        )

    unique_markers: list[str] = []
    for marker in markers:
        if marker not in unique_markers:
            unique_markers.append(marker)

    parts = [FOLD_HEADER, "\n".join(clean_lines), FOLD_MARKERS_HEADER]
    if unique_markers:
        parts.append("\n".join(unique_markers))
    return Message(role=Role.USER, content="\n".join(parts))


async def fold_session(session_id: str, policy: FoldPolicy | None) -> bool:
    """轮写回之后的独立折叠步骤：达阈值就整卷压一条，返回是否折了。

    策略 ``None`` = 完全不折叠（连 load 都不做，chat 等调用方零感知）。整段
    fail-open：任何失败（回调炸 / 超时 / 写库炸）只 warning + 返回 False，
    transcript 原样不动——本轮写回已 durable 落定，折叠失败下轮再试，硬截断
    兜底。调用方须在与写回相同的串行窗口内调（同 session 无并发写，与
    ``append_session`` 的串行约定一致）。

    版本 CAS 兜底（codex T3 必改 2）：串行窗口靠单飞锁，但锁 TTL（600s）不续租
    ——本体轮 + 沉淀 LLM（最长 120s）极端超过 TTL 时锁过期、新一轮进入并
    append，旧 fold 的整卷覆写会把新 append 吞掉。所以 load 时记下当时的最新
    ver，``replace_session(expected_ver=)`` 条件写入（校验与写入是同一条 SQL，
    无 TOCTOU）：期间有人 append → 放弃本次折叠（warning + False，下次到阈值
    再折），新 append 一条不丢。
    """
    if policy is None:
        return False
    try:
        messages, loaded_ver = await load_session_versioned(session_id)
        if len(messages) < policy.trigger_messages:
            return False

        # 旧折叠消息（若有，必在卷首——折叠产物之后只会 append 新轮）拆出旧沉淀
        # 与旧 marker；其余是本次要折叠的轮。
        if messages and is_fold_message(messages[0]):
            prior_sediment, markers = split_fold_message(messages[0])
            rounds = messages[1:]
        else:
            prior_sediment = None
            markers = []
            rounds = messages

        # 铁律①：marker 并集由代码做（旧载荷 ∪ 新轮提取，保序去重），绝不经回调。
        for marker in extract_round_markers(rounds):
            if marker not in markers:
                markers.append(marker)

        sediment = await policy.write_sediment(prior_sediment, rounds)
        folded = build_fold_message(sediment, markers)
        if not await replace_session(session_id, [folded], expected_ver=loaded_ver):
            # 沉淀期间 transcript 版本前进了（锁过期、新一轮 append）：整卷覆写
            # 基于的是过时的卷，落库会吞掉新 append。放弃本次折叠（fail-open，
            # 新轮原样在、下次到阈值再折），不静默。
            logger.warning(
                "agent session %s fold abandoned: transcript advanced past "
                "ver %d during sedimentation (concurrent append, e.g. expired "
                "serialisation lock); fail-open, will refold at a later "
                "threshold",
                session_id,
                loaded_ver,
            )
            return False
        return True
    except Exception:
        logger.warning(
            "agent session %s fold failed; fail-open: transcript left as-is, "
            "retry on a later round (hard caps still guard)",
            session_id,
            exc_info=True,
        )
        return False
