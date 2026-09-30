"""跨轮连续的上下文 —— 一个 agent 每一轮的输入和产出存下来，下一轮接在输入前面。

一轮跑完，这一轮喂进去的那条 USER 消息、模型说的每一句、每一次工具调用和工具返回，
原样存成下一版；下一轮把它们接在这一轮的输入前面。agent 因此不是每一轮从头开始，而是
接着上次往下走。

**这是基础层。** 任何 App 的 agent 都可以用，它不 import 任何 App 的代码，也不认识任何
业务上的时间（谁的"一天"从几点起）。它提供三样：

  * **写入**：:func:`commit_transcript` 带版本做 CAS，别人在中间写过就抛
    :class:`TranscriptConflict`。读用 :func:`app.agent.session.load_session`，它交回的
    版本号原样带到写入这一步。
  * **裁剪**：:func:`trim_for_round`（喂给模型之前）和 :func:`next_transcript`（存下去
    之前）。按调用方给的 :class:`TrimPolicy` 和素材工具表裁。
  * **界桩**：清理点和"上一轮没存下来"各立一根 USER 消息，带时刻和调用方渲染好的那段
    状态，作为往后那一段的新起点。

**调用方自己定的**：存储键（带不带泳道、分不分人）、五个阈值、哪些工具的返回算素材、
界桩上铺的那段状态、写失败之后这一轮算不算数。这一层对这些一个默认值都不给：给了默认
值的话，接进来的下一个调用方一个阈值都不写、一只手都不分类也照跑，跑的是谁设计的那一套
没人说得清，而且一句报错都没有。

存储
----

存在 :class:`app.domain.session_transcript.SessionTranscript` 上，每一版一行，读永远
取最新那一版（:mod:`app.agent.session`）。进程里不存任何副本，杀掉 pod 重启照样接上。

**存储层不做任何截断。** 裁剪只在这里一处：两套同时生效的话，存储层会先砍掉策略想留
的东西，而调用方拿到的返回值一切正常，排查时看不出来。

**写入是 CAS。** 调用方要保证同一条上下文不会有两轮同时在跑；:class:`TranscriptConflict`
是这个保证破了之后的一道门，把"看不见的互相覆盖"变成一行看得见的错误。它不是多副本并发
写的许可证。

**写入跑在调用方给的事务里**（``session`` 必须显式给）。它跟调用方的哪些写入一起提交，
由调用方决定；给 ``None`` 就是它单独开一个事务提交，跟别的写入都不绑。

裁剪规则
--------

**两档时长，固定时刻清理。** 工具返回里的素材留 ``material_minutes``，其余（每轮的输入、
模型自己说的话、不算素材的工具返回）留 ``own_minutes``。清理只发生在清理点上：一张从
Unix 纪元起算、每格 ``cleanup_minutes`` 分钟的固定网格（:func:`_cleanup_instant`）。
两次清理之间截止线一动不动，前缀逐字节稳定，前缀缓存才有得命中；滑动窗口会每轮都改
上下文开头。周期整除 60 时清理点落在整点上（任何整小时偏移的时区里都是）。

**"保留 1 小时"在整点清理下实际是 1–2 小时，这是设计不是 bug。** 年龄的下界是一条消息
右边第一根界桩的时刻，所以刚过清理点写下的东西要等到下一个清理点之后才可能被裁。

**以一次完整的工具调用为单位裁。** 没有结果的工具调用会被 provider 拒掉整个请求，同一
轮里多个调用和多个结果也必须逐个对上。所以调用还在保留期内时只把过期的**载荷**换成一句
写死的短语（:data:`MATERIAL_TRIMMED`），消息结构一条不动；整组过期时调用和它的全部结果
一起删。**换成写死的短语，不是概括**：概括会留下一个可能已经错了的版本，而原文没了，错
了没人知道。

**图片块比文本先走。** 图片地址是有寿命的预签名 URL（:data:`PICTURE_URL_MINUTES`），
adapter 回放历史时会把 http(s) 地址重新下载，过期就在模型请求之前抛错。所以图片块只活在
最新那一代：一根界桩立起来，它之前那一代的图片就换成 :data:`PICTURE_TRIMMED`，同一条
消息里的文字照留。一张图最长活一个清理周期加一轮间隔，:data:`MAX_CLEANUP_MINUTES` 守住
它和 :data:`PICTURE_URL_MINUTES` 之间的余量。

**每次清理立一根界桩，把调用方给的状态铺进去。** 旧东西被裁掉之后那段经历只剩调用方自己
的记录里还有，界桩把"现在"重铺成新起点。空历史（第一次跑、刚清过库）也立一根。界桩同时
是**分代的边界**：每条消息的年龄下界就是它右边第一根界桩的时刻，不需要给每条消息单独存
时刻。界桩之前那一代（还没有任何界桩时写下的东西）没有上界，一律留着。

**上一轮没存下来时立的是另一种界桩**（:data:`GAP_HEAD`）。历史一条不丢，少的是中间那一
轮的经过；文案因此跟清理那种分开，说的是"接不上"而不是"看不到"。判据由调用方给
（``lost_last_round``）。

**裁在模型调用之前**（:func:`trim_for_round`），存下去之前只做硬顶兜底
（:func:`next_transcript`）。只在收尾裁的话，一段带着过期图片地址的历史永远轮不到被
裁——每一轮都在 adapter 下载那一步抛错，收尾走不到。

**硬顶是兜底，不是主路，判两次。** 估算超过 ``hard_cap_tokens`` 就从最老的组开始整组丢
到 ``trim_target_tokens`` 以下，并记一行日志。存下去那次不动这一轮自己的消息（丢掉刚发生
的事等于这一轮白跑），所以一轮自己就超了的时候它照样写下去；喂之前那次没有这个豁免，会
把它裁回顶以下。少了后者就是死循环：那一份下一轮读回来仍然超限、模型当场失败、失败就不
提交、再读到同一份。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agent.neutral import ContentBlock, Message, Role
from app.agent.session import replace_session

logger = logging.getLogger(__name__)


class TranscriptConflict(RuntimeError):
    """这条上下文在这一轮跑的时候被别人写过了。

    调用方保证同一条上下文不会有两轮同时在跑，所以正常永远撞不上。撞上就说明那个前提
    破了（多副本、或者有人绕开了调用方的排他占用）。默默覆盖等于把另一个写入方刚写下的
    一整段丢掉，而且没有任何痕迹，所以这里必须抛。
    """


async def commit_transcript(
    key: str,
    messages: list[Message],
    *,
    expected_ver: int,
    session: Any,
) -> None:
    """把这一轮结束后的完整上下文写成下一版，落在调用方的事务里。

    ``messages`` 是**完整的新上下文**（:func:`next_transcript` 的结果），不是增量。
    ``expected_ver`` 是 :func:`app.agent.session.load_session` 读到的那一版；库里已经
    不是它了就抛 :class:`TranscriptConflict`，一行都不写。写失败（CAS 没落地、或者 PG
    抛错）一律往外抛，怎么处理由调用方定。``session`` 是调用方的事务；``None`` 表示单独
    提交。
    """
    landed = await replace_session(
        key, messages, expected_ver=expected_ver, session=session
    )
    if not landed:
        raise TranscriptConflict(
            f"上下文 {key} 在这一轮跑的时候被别人写过了（读到的是 ver={expected_ver}）"
            f"—— 同一条上下文有两轮在并发跑，调用方的排他占用破了"
        )


# 过期载荷换成的那句话，和图片块换成的那句话。写死，不是概括。
MATERIAL_TRIMMED = "（这一段你当时读过，现在不在眼前了。还要就再去看一次。）"
PICTURE_TRIMMED = "（这张图不在你眼前了。还要看就再拿出来一次。）"

# 图片地址的寿命：``tos_client.get_file_url`` 的签名 90 分钟就过期。
PICTURE_URL_MINUTES = 90

# 清理周期的上限。一张图最长活一个清理周期加一轮间隔：60 分钟的周期留给轮间隔的余量是
# 30 分钟。要把周期配得更长，得先解决"回放时地址已经死了"这件事本身，不能只调这个数。
MAX_CLEANUP_MINUTES = 60

# token 估算。没有能离线跑的 tokenizer（gemini 的 count_tokens 是一次网络调用，不能
# 放在每轮写库的路上），所以按字节估，而且**一律往高了估**——估低了才会真的撞上模型
# 的上限，那时是整轮抛错。
#
#   * 每 3 个 UTF-8 字节算 1 个 token。中日文一个字 3 字节 ≈ 1 token，而 SentencePiece
#     常把常用词并成一个，所以这是高估；ASCII 实测约 4 字符 1 token，按 3 字节算同样高估
#   * 一张图按 2600 算：gemini 每 768×768 一块 258 token，2048×2048 是 9 块 ≈ 2322
#   * 每条消息再加 8，算角色、id 这些框架开销
_BYTES_PER_TOKEN = 3
_PICTURE_TOKENS = 2600
_FRAME_TOKENS = 8

# 界桩那条消息的开头，两种。只有这里写 USER 消息用这两个开头：
#
#   * :data:`CHECKPOINT_HEAD`  固定时刻的清理，往前那一段真的不在了；
#   * :data:`GAP_HEAD`         上一轮的上下文没落地，往前那一段还在、中间少了一轮。
#
# 两种在结构上是同一回事 —— 都带一个时刻、都重铺一遍状态 —— 所以都算这一代的界桩。
# 文案必须分开：缺口那次眼前的历史一条没少，套用"再往前的那一段不在你眼前了"就是
# 往它眼前塞一句假话。
CHECKPOINT_HEAD = "【上下文清理 "
GAP_HEAD = "【上一轮没存下来 "
_MARKER_TAIL = "】"
_MARKER_HEADS = (CHECKPOINT_HEAD, GAP_HEAD)

# 清理网格的起点。固定在纪元上，不跟任何业务上的"一天"对齐。
_GRID_ORIGIN = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class TrimPolicy:
    """裁剪的五个阈值 —— 只有形状，值由调用方给，这里不放默认值、也不从任何地方读。

    这五个数是"这个 agent 是什么东西"的一部分，不是运行时旋钮：每个调用方在自己的
    模块里写死自己那一份，并用用例钉住五条约束：两档时长都是正数；``own_minutes`` 不小于
    ``material_minutes``；``cleanup_minutes`` 落在 1 到 :data:`MAX_CLEANUP_MINUTES` 之间；
    硬顶和裁剪目标都是正数；裁剪目标严格小于硬顶。
    """

    material_minutes: int
    own_minutes: int
    cleanup_minutes: int
    hard_cap_tokens: int
    trim_target_tokens: int


def estimate_tokens(messages: list[Message]) -> int:
    """这一份上下文大概多少 token —— 往高了估。

    只算上下文本身：SYSTEM 正文和工具定义不在这份列表里，它们是每次请求的固定开销，
    设硬顶时要在模型的 context 上限之外给它们和这一轮的新增留出余量。
    """
    return sum(_message_tokens(m) for m in messages)


def _text_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // _BYTES_PER_TOKEN)


def _message_tokens(message: Message) -> int:
    total = _FRAME_TOKENS
    content = message.content
    if isinstance(content, str):
        total += _text_tokens(content)
    else:
        for block in content:
            if block.type == "text":
                total += _text_tokens(block.text or "")
            else:
                total += _PICTURE_TOKENS
    total += _text_tokens(message.thought_text())
    for call in message.tool_calls:
        total += _text_tokens(call.name)
        total += _text_tokens(json.dumps(call.arguments, ensure_ascii=False))
    return total


def _cleanup_instant(now: datetime, minutes: int) -> datetime:
    """``now`` 之前最近的那个清理点：从纪元起算的 ``minutes`` 分钟网格，时区沿用 ``now``。

    时区沿用调用方的，界桩上印出来的时刻才跟调用方其余地方的时刻是同一种写法。
    """
    step = timedelta(minutes=minutes)
    instant = _GRID_ORIGIN + (now - _GRID_ORIGIN) // step * step
    return instant.astimezone(now.tzinfo)


def _marker(head: str, at: datetime, what_happened: str, state: str) -> Message:
    """一根界桩：发生了什么 + 那个时刻 + 此刻的状态，作为往后那一段的新起点。"""
    return Message(
        role=Role.USER,
        content=(
            f"{head}{at.isoformat()}{_MARKER_TAIL}\n{what_happened}你现在：\n\n{state}"
        ),
    )


def _checkpoint(at: datetime, state: str) -> Message:
    """固定时刻清理立的那根：再往前的东西这一下真的从眼前走了。"""
    return _marker(
        CHECKPOINT_HEAD, at, "再往前的那一段不在你眼前了，只剩你自己记下来的。", state
    )


def _gap_marker(at: datetime, state: str) -> Message:
    """上一轮的上下文没落地时立的那根：往前那一段一条没少，少的是中间那一轮的经过。"""
    return _marker(
        GAP_HEAD,
        at,
        "上一轮你做过说过的没能存下来，往上那一段停在它**之前** —— 中间那一轮的"
        "经过接不回来了，它留下的东西在下面这份状态里。",
        state,
    )


def _marker_at(message: Message) -> datetime | None:
    """这条是界桩吗（两种都算）；是就给出它的时刻。"""
    if message.role is not Role.USER or not isinstance(message.content, str):
        return None
    head = next((h for h in _MARKER_HEADS if message.content.startswith(h)), None)
    if head is None:
        return None
    end = message.content.find(_MARKER_TAIL, len(head))
    if end < 0:
        return None
    try:
        return datetime.fromisoformat(message.content[len(head) : end])
    except ValueError:
        return None


def _groups(messages: list[Message]) -> list[list[int]]:
    """把消息切成"一次完整的工具调用"：带调用的那条 ASSISTANT + 紧跟的全部 TOOL。

    其余每条自成一组。整组留、整组删，就不会出现没有结果的调用。
    """
    groups: list[list[int]] = []
    i = 0
    while i < len(messages):
        group = [i]
        if messages[i].role is Role.ASSISTANT and messages[i].tool_calls:
            j = i + 1
            while j < len(messages) and messages[j].role is Role.TOOL:
                group.append(j)
                j += 1
            i = j
        else:
            i += 1
        groups.append(group)
    return groups


def _bounds(messages: list[Message]) -> list[datetime | None]:
    """每条消息的"最晚写于"：它右边第一根界桩的时刻；``None`` = 在最新那一代里。"""
    nearest: datetime | None = None
    out: list[datetime | None] = [None] * len(messages)
    for i in range(len(messages) - 1, -1, -1):
        out[i] = nearest
        at = _marker_at(messages[i])
        if at is not None:
            nearest = at
    return out


def _without_pictures(message: Message) -> Message:
    """图片块换成一句话，别的一个字不动。

    换成**文本块**而不是整个丢掉：调用方的正文里可能逐张写着图片编号，块数一少就对不上
    了。``replace`` 而不是逐字段重建：契约就是「只换 content」，逐字段抄漏一个（比如思考
    段和它们的签名）下一轮请求就会被 provider 拒掉。
    """
    content = message.content
    if not isinstance(content, list) or all(b.type == "text" for b in content):
        return message
    return replace(
        message,
        content=[
            b if b.type == "text" else ContentBlock.from_text(PICTURE_TRIMMED)
            for b in content
        ],
    )


def _faded(
    message: Message, *, name: str | None, material_tools: frozenset[str]
) -> Message:
    """过期的素材载荷换成写死的短语；不是素材的只去掉图片。消息结构一条不动。"""
    if message.role is not Role.TOOL or name not in material_tools:
        return _without_pictures(message)
    return Message(
        role=Role.TOOL, content=MATERIAL_TRIMMED, tool_call_id=message.tool_call_id
    )


def _clean(
    messages: list[Message],
    *,
    at: datetime,
    policy: TrimPolicy,
    material_tools: frozenset[str],
) -> list[Message]:
    """按两档时长裁一遍。整组过期整组删，没过期只换过期的载荷，旧一代的图片一律换掉。"""
    bounds = _bounds(messages)
    names = {call.id: call.name for m in messages for call in m.tool_calls}
    own = timedelta(minutes=policy.own_minutes)
    material = timedelta(minutes=policy.material_minutes)

    kept: list[Message] = []
    for group in _groups(messages):
        bound = bounds[group[0]]
        if bound is None:
            kept.extend(messages[i] for i in group)
            continue
        age = at - bound
        if age >= own:
            continue
        if age >= material:
            kept.extend(
                _faded(
                    messages[i],
                    name=names.get(messages[i].tool_call_id or ""),
                    material_tools=material_tools,
                )
                for i in group
            )
        else:
            kept.extend(_without_pictures(messages[i]) for i in group)
    return kept


def _under_cap(
    messages: list[Message], *, floor: int, policy: TrimPolicy
) -> list[Message]:
    """撞上硬顶就从最老的组开始整组丢，丢到裁剪目标以下，并且一定留下一行日志。

    末尾 ``floor`` 条一条都不丢。
    """
    total = estimate_tokens(messages)
    if total <= policy.hard_cap_tokens:
        return messages

    tail_from = len(messages) - floor
    dropped: set[int] = set()
    running = total
    for group in _groups(messages):
        if running <= policy.trim_target_tokens or group[-1] >= tail_from:
            break
        dropped.update(group)
        running -= sum(_message_tokens(messages[i]) for i in group)

    kept = [m for i, m in enumerate(messages) if i not in dropped]
    line = "上下文撞到硬顶：估 %d token > %d，裁到 %d token（%d 条 → %d 条）"
    args = (total, policy.hard_cap_tokens, running, len(messages), len(kept))
    if running > policy.hard_cap_tokens:
        logger.error(line + "；这一轮自己就超了，只能原样写下去", *args)
    else:
        logger.warning(line, *args)
    return kept


def _crossed(history: list[Message], at: datetime) -> bool:
    """这一轮跨过清理点了吗 —— 最后一根界桩比这个清理点早就是跨过了；一根都没有也算。"""
    for message in reversed(history):
        last = _marker_at(message)
        if last is not None:
            return last < at
    return True


def trim_for_round(
    history: list[Message],
    *,
    now: datetime,
    state: str,
    policy: TrimPolicy,
    material_tools: frozenset[str],
    lost_last_round: bool = False,
) -> list[Message]:
    """这一轮该喂给模型的那份历史。

    跨过清理点（或者历史是空的）：立一根清理界桩，再按两档时长裁一遍。没跨过但
    ``lost_last_round``：立一根时刻为 ``now`` 的缺口界桩，历史一条不丢。两样都没有：
    原样还回来，一个字节都不动（硬顶除外）。

    ``state`` 是调用方渲染好的"此刻的状态"，只铺在界桩上。``material_tools`` 是哪些工具
    的返回算素材。``lost_last_round`` 是"上一轮的上下文没落地"，判据归调用方。

    界桩先立再裁：它同时是这一代的上界，立完再裁，这一代的图片当场就走。刚立的那根不许
    被硬顶裁掉：它是这一轮唯一一份"你现在"。
    """
    at = _cleanup_instant(now, policy.cleanup_minutes)
    staged = list(history)
    laid = _crossed(history, at)
    if laid:
        staged.append(_checkpoint(at, state))
    elif lost_last_round:
        # 跨清理点那根已经重铺过状态了，两根一起立没有意义。
        staged.append(_gap_marker(now, state))
        laid = True
    cleaned = _clean(staged, at=at, policy=policy, material_tools=material_tools)
    return _under_cap(cleaned, floor=1 if laid else 0, policy=policy)


def next_transcript(
    history: list[Message],
    produced: list[Message],
    *,
    policy: TrimPolicy,
) -> list[Message]:
    """这一轮结束后该存下来的完整上下文，直接交给 :func:`commit_transcript`。

    ``history`` 是 :func:`trim_for_round` 裁过、这一轮真的喂给了模型的那一份，
    ``produced`` 是这一轮的输入和模型产出的每一条。两档时长在上一步已经裁完，这里只剩
    硬顶兜底，而且 ``produced`` 一条都不丢。
    """
    if not produced:
        raise ValueError("这一轮一条消息都没有 —— 没有可写下去的上下文")
    return _under_cap([*history, *produced], floor=len(produced), policy=policy)
