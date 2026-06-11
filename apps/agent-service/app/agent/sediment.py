"""沉淀 agent — 被折叠的当天轮次整篇重写成「她自己的回忆」（沉淀 Task 2）.

:mod:`app.agent.session_fold` 是折叠机制（何时折、折成什么形态、marker 怎么保全），
它把「沉淀正文从哪来」留成 ``SedimentWriter`` 回调契约；本模块是该契约的两个实现：

  * **life**（:func:`build_life_fold_policy`）：以该 persona 第一人称，把（旧沉淀 +
    被折叠的轮消息）整篇重写成「这一天到此刻为止」的当天回忆——langfuse prompt
    ``life_sediment``，prompt_vars 契约 = {persona_name, persona_lite}（与
    life_day_review 同款薄契约）。
  * **world**（:func:`build_world_fold_policy`）：以推演者口吻整篇重写成「这一天到
    此刻为止世界怎么流过来」的客观梗概——langfuse prompt ``world_sediment``，
    prompt_vars = {}（零 vars）。

机制硬约束（照 :mod:`app.world.reflection` / :mod:`app.life.review` 的范式）：

  * **无会话 + 无工具 + offline-model + max_retries=1**：一次 LLM 调用整篇重写。
    ``run`` 绝不传 session_id——沉淀正是在改写这条 transcript，续接它即自指。
    langfuse 归组走 ``AgentContext.session_id``（只做 trace 标签）。
  * **硬超时**：:data:`SEDIMENT_TIMEOUT_SECONDS` 的 ``asyncio.wait_for`` 包住 LLM
    调用（远小于 life / world 单飞锁 TTL 600s）。超时 / LLM 抛错 / 空产出都**向上
    抛**——fail-open 收口在 ``fold_session``（本版不折、transcript 原样不动、下轮
    再试），这里不吞。空产出必须拦：空沉淀替掉整卷 = 真失忆，正是本块要消灭的。
  * **成本独立入账、不嵌套污染**：回调内自带 ``collect_usage`` 作用域包住沉淀调用，
    actor = ``{persona_id}:sediment`` / ``world:sediment``；round_id 从
    (session_id, 触发折叠的那轮 round_id) 派生幂等——durable 重投同一轮再触发折叠
    时 ``insert_idempotent`` 不重复计。调用点（life/world 轮收口）必须在轮自己的
    ``collect_usage`` 作用域之外，沉淀的 token 绝不算进本体 actor。
  * **证据条目控制、绝不字符截断**：被折叠的轮按条目逐条铺开（USER=当时的输入 /
    感知、ASSISTANT=当时所想所写；TOOL 机械确认不进），round marker 摘干净（机制
    载荷不是内容，铁律②）。条目数天然被折叠阈值（100 条）封顶，不另设截断。

写成什么样、留哪些细节由沉淀 agent 自己判断——写作纪律（口吻 / 人设 / 纪律）在
langfuse prompt 层约束，本模块的 instruction 只承载任务语义与输出形态，零剧情
事实（赤尾宪法）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.session_fold import FoldPolicy, strip_round_markers
from app.agent.trace import collect_usage
from app.domain.thinking_cost import record_round_cost
from app.infra import cst_time
from app.memory._persona import load_persona  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# 沉淀调用的硬超时：一次离线模型整篇重写（无工具循环），分钟级足够；必须远小于
# life / world 单飞锁 TTL（600s）——轮收口后的折叠步骤仍在锁内，绝不把锁拖到过期
# （过期后新一轮并发写同一 transcript，fold 的读改写会丢轮）。超时抛 TimeoutError
# 走 fold_session 的 fail-open。
SEDIMENT_TIMEOUT_SECONDS = 120

# 两口吻各一个独立 AgentConfig：prompt id 钉为 langfuse 上的 "life_sediment" /
# "world_sediment"（prompt 本体由主会话发布；prompt_vars 契约见模块 docstring）。
# prompt 缺失 = 沉淀失败 = fold_session fail-open（本版不折、下轮再试）。
_LIFE_SEDIMENT_CFG = AgentConfig("life_sediment", "offline-model", "life-sediment")
_WORLD_SEDIMENT_CFG = AgentConfig("world_sediment", "offline-model", "world-sediment")


def life_sediment_instruction() -> str:
    """life 沉淀的任务指令（代码侧只承载任务语义与输出形态；零剧情事实——宪法）。"""
    return (
        "这一天过到现在，前面的经历要沉成你随身记得的回忆了。下面给你两样东西：你"
        "此前已经沉淀过的当天回忆（可能还没有），和这之后你新经历的一段原始意识流。"
        "以你本人的第一人称，把两样合在一起整篇重写成一段「这一天到此刻为止」的"
        "回忆——这一天到现在留下了什么：发生过的事、触动你的瞬间、还悬着没落地的"
        "念头。只写真实经历过的，绝不编没发生的事。新的一版取代旧的一版。\n\n"
        "直接输出重写后的回忆正文——不要标题、不要列表、不要解释说明、不要任何"
        "机器标记。"
    )


def world_sediment_instruction() -> str:
    """world 沉淀的任务指令（推演者口吻、纯客观；零剧情事实——宪法）。"""
    return (
        "你是这个世界的推演层。今天到现在为止的推演记录要压成一段「这一天到此刻」"
        "的世界梗概了。下面给你两样东西：此前已经沉淀过的当天梗概（可能还没有），"
        "和这之后新的推演记录。以推演者的口吻，把两样合在一起整篇重写成一段「这一天"
        "到此刻为止」世界怎么流过来的客观叙述——谁大致做了什么、世界发生了哪些客观"
        "变化、现在停在什么样子。只写记录里真实出现过的，绝不编造，也绝不写情绪、"
        "心情或主观解读。新的一版取代旧的一版。\n\n"
        "直接输出重写后的梗概正文——不要标题、不要列表、不要解释说明、不要任何"
        "机器标记。"
    )


def _sediment_round_id(session_id: str, trigger_round_id: str) -> str:
    """沉淀成本的 round_id：从 (session_id, 触发折叠的那轮) 派生幂等。

    durable 重投 / 整轮重试会让同一轮的收口再触发一次折叠——派生出同一 round_id，
    ``ThinkingTokensSpent`` 的 ``insert_idempotent`` 按 (lane, actor, round_id)
    去重、不重复计成本。换一轮触发（下次再折）是另一件事、另一个 id。
    """
    return uuid.uuid5(
        uuid.NAMESPACE_OID, f"sediment\x1f{session_id}\x1f{trigger_round_id}"
    ).hex


def _rounds_evidence(
    rounds: list[Message], *, user_label: str, assistant_label: str
) -> str:
    """被折叠的轮 → 逐条铺开的证据段（条目级取舍、绝不字符截断）。

    USER / ASSISTANT 各按口吻标签呈现；TOOL 机械确认文本（"状态已更新"）不进；
    round marker 是 turn 幂等的机制载荷、不是内容，摘干净（铁律②：喂进去只会被
    模型复述、再被 build_fold_message 净化，徒增噪声）。各段原文里的时刻行
    （life 的「现在是 X」/ world 的【现实此刻】）原样保留——这就是每段的时间标注。
    """
    lines: list[str] = []
    for m in rounds:
        if m.role not in (Role.USER, Role.ASSISTANT):
            continue
        text = strip_round_markers(m.text()).strip()
        if not text:
            continue
        label = user_label if m.role == Role.USER else assistant_label
        lines.append(f"〔{label}〕{text}")
    if not lines:
        return "（这段时间没有留下可读的记录。）"
    return "\n".join(lines)


def _life_sediment_messages(
    *, now_iso: str, prior: str | None, rounds: list[Message]
) -> list[Message]:
    """life 沉淀的输入：单条 user 消息（无会话、一次喂全；模板零剧情事实）。"""
    prior_text = (
        prior
        if prior is not None and prior.strip()
        else "（这是这一天的第一次沉淀——之前还没有写下过当天的回忆。）"
    )
    evidence = _rounds_evidence(
        rounds, user_label="你当时感知到", assistant_label="你当时想着 / 说做了"
    )
    user_content = (
        f"{life_sediment_instruction()}\n\n"
        f"【现实此刻】{now_iso}\n\n"
        f"【你此前已沉淀的当天回忆】\n{prior_text}\n\n"
        f"【这之后你新经历的原始意识流】（按先后排列；各段里标注的时刻就是它发生"
        f"的时刻）\n{evidence}"
    )
    return [Message(role=Role.USER, content=user_content)]


def _world_sediment_messages(
    *, now_iso: str, prior: str | None, rounds: list[Message]
) -> list[Message]:
    """world 沉淀的输入：单条 user 消息（推演口吻的证据标签）。"""
    prior_text = (
        prior
        if prior is not None and prior.strip()
        else "（这是这一天的第一次沉淀——之前还没有写下过当天的梗概。）"
    )
    evidence = _rounds_evidence(
        rounds, user_label="当时给你的输入", assistant_label="你当时的推演"
    )
    user_content = (
        f"{world_sediment_instruction()}\n\n"
        f"【现实此刻】{now_iso}\n\n"
        f"【此前已沉淀的当天梗概】\n{prior_text}\n\n"
        f"【这之后的推演记录】（按先后排列；各段里标注的时刻就是它发生的时刻）\n"
        f"{evidence}"
    )
    return [Message(role=Role.USER, content=user_content)]


async def _run_sediment(
    *,
    cfg: AgentConfig,
    messages: list[Message],
    prompt_vars: dict[str, str],
    context: AgentContext,
    lane: str,
    actor: str,
    cost_round_id: str,
    observed_at: str,
) -> str:
    """跑一次沉淀调用：硬超时 + 独立成本作用域 + 空产出拦截。失败向上抛（fail-open 在 fold_session）。

    成本与 run 成败的口径：run 正常返回（含空产出）→ token 真烧了、照记（同
    review「无论成败都记」）；run 抛错 / 超时被掐 → 不记（usage 不完整，同
    reflection 失败路径）。
    """
    with collect_usage() as usage:
        result = await asyncio.wait_for(
            Agent(cfg).run(
                messages,
                prompt_vars=prompt_vars,
                context=context,
                max_retries=1,
            ),
            timeout=SEDIMENT_TIMEOUT_SECONDS,
        )
    await record_round_cost(
        lane=lane,
        actor=actor,
        round_id=cost_round_id,
        usage=usage,
        observed_at=observed_at,
    )
    sediment = result.text().strip()
    if not sediment:
        # 机制安全阀（不是内容检测器）：空沉淀替掉整卷 = 真失忆。抛出去走
        # fold_session 的 fail-open——本版不折、下轮再试。
        raise ValueError("sediment agent returned empty text; keep transcript unfolded")
    return sediment


def build_life_fold_policy(
    *, lane: str, persona_id: str, session_id: str, round_id: str
) -> FoldPolicy:
    """life 轮收口用的折叠策略：沉淀回调 = 该 persona 第一人称的当天回忆。

    ``round_id`` 是触发折叠的那轮 life round（成本 round_id 从它派生幂等）；
    ``session_id`` 是她当天的意识流 transcript key，兼作 langfuse 归组标签。
    """
    cost_round_id = _sediment_round_id(session_id, round_id)

    async def write_life_sediment(prior: str | None, rounds: list[Message]) -> str:
        now = cst_time.now_cst()
        pc = await load_persona(persona_id)
        return await _run_sediment(
            cfg=_LIFE_SEDIMENT_CFG,
            messages=_life_sediment_messages(
                now_iso=now.isoformat(), prior=prior, rounds=rounds
            ),
            prompt_vars={
                "persona_name": pc.display_name,
                "persona_lite": pc.persona_lite,
            },
            context=AgentContext(persona_id=persona_id, session_id=session_id),
            lane=lane,
            actor=f"{persona_id}:sediment",
            cost_round_id=cost_round_id,
            observed_at=now.isoformat(),
        )

    return FoldPolicy(write_sediment=write_life_sediment)


def build_world_fold_policy(
    *, lane: str, session_id: str, round_id: str
) -> FoldPolicy:
    """world 轮收口用的折叠策略：沉淀回调 = 推演者口吻的当天世界梗概（零 prompt_vars）。"""
    cost_round_id = _sediment_round_id(session_id, round_id)

    async def write_world_sediment(prior: str | None, rounds: list[Message]) -> str:
        now = cst_time.now_cst()
        return await _run_sediment(
            cfg=_WORLD_SEDIMENT_CFG,
            messages=_world_sediment_messages(
                now_iso=now.isoformat(), prior=prior, rounds=rounds
            ),
            prompt_vars={},
            context=AgentContext(session_id=session_id),
            lane=lane,
            actor="world:sediment",
            cost_round_id=cost_round_id,
            observed_at=now.isoformat(),
        )

    return FoldPolicy(write_sediment=write_world_sediment)
