"""ThinkingTokensSpent —— 一轮 world/life 思考用了多少 token，落 durable PG（观测刀）。

**为什么有这张表（已实证）**：每轮 world/life 思考的 token 成本现在只经
``span.update(usage_details=...)`` 喂给 langfuse，而 langfuse 是 best-effort、会
**系统性丢 durable 工具的 trace**（coe 一夜实测：akao 在 PG ``data_act_performed``
有 45 条 act，langfuse 名下 0 条 ``tool.act`` trace；chinagi/ayana 的 act trace 也
只留存约 60%）。基于 langfuse 的成本统计因此严重失真——角色成本被低估、world 占比被
高估。结论：PG 是可靠真相，langfuse 会丢。所以"谁做了什么"PG 已全（act_performed），
**缺的是成本**——这张表把每轮 token 如实落 durable PG，按 actor 可聚合查、不依赖会丢
的 langfuse。

**这是观测 / 机制层（赤尾设计宪法）**：只如实记录一轮烧了多少 token，绝不引入任何
阈值 / 计数器 / 定时器去"控制"角色或世界的行为。``model_calls`` 是本轮 LLM 调用次数
（工具循环可能多轮 model 调用），只是观测维度、不是控制闸。

**自然键 (lane, actor, round_id)**：
  * ``lane`` —— 泳道隔离硬约束（同其它 durable Data：runtime 持久化不自动加 lane，
    不显式带上 coe / ppe 就会污染 prod 的成本记录）。
  * ``actor`` —— "world" 或某 persona_id，可按它聚合"谁花了多少"。
  * ``round_id`` —— 本轮标识（与 engine 的 turn 幂等 round_id 同源）。

用 ``insert_idempotent``（非 ``insert_append``）：整轮重试 / durable 重投会用同一
``(lane, actor, round_id)`` 再记一次，``insert_idempotent`` 是 ON CONFLICT DO
NOTHING、重投不重复计成本（成本记录天然幂等：同一轮就该只有一行）。没有 ``Version``
—— 一轮的成本是个确定事实、不需要版本演进。

字段都是标量（str / int），是这张观测表的形态选择——一轮成本就这几个标量维度
（framework 已支持 dict/list → JSONB，这里不放结构化字段是设计、不是限制）。
``observed_at`` 而非 ``recorded_at`` 之类带 ``_at`` 的保留名冲突——``created_at`` /
``updated_at`` 是 migrator 自动加的保留列，业务字段绕开。

**索引现状（已知、当前足够）**：migrator 只给幂等去重的 ``dedup_hash`` 建唯一索引；
``(lane, actor, observed_at)`` 这类"按时间段聚合查某 actor 花了多少"的复合索引**没建**。
当前量极小（每轮一行、一天几百行），全表扫够快、不需要额外索引。等真要按时间区间做成本
聚合查（数据量上来后扫表变慢）再补这个索引。
"""

from __future__ import annotations

import logging
from typing import Annotated

# insert_idempotent imported module-level so tests can monkeypatch it.
from app.runtime.data import Data, Key
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)


class ThinkingTokensSpent(Data):
    """某 actor 一轮思考的 token 用量。自然键 (lane, actor, round_id)，幂等去重。

    ``actor`` = "world" 或某 persona_id。``round_id`` 与 engine turn 幂等同源。token
    各维度：input / output / total / cached + 本轮 LLM 调用次数 ``model_calls``。
    """

    lane: Annotated[str, Key]
    actor: Annotated[str, Key]
    round_id: Annotated[str, Key]
    input_tokens: int            # 本轮累计 prompt token
    output_tokens: int           # 本轮累计 completion token
    total_tokens: int            # 本轮累计 total token
    cached_tokens: int = 0       # 本轮命中 prompt cache 的 token（cache_read_input_tokens）
    model_calls: int = 0         # 本轮 LLM 调用次数（工具循环可能多轮）
    observed_at: str             # 这轮成本观测到的时刻 (ISO8601)


async def record_thinking_tokens(
    *,
    lane: str,
    actor: str,
    round_id: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cached_tokens: int,
    model_calls: int,
    observed_at: str,
) -> None:
    """把某 actor 一轮思考的累计 token 落 ``ThinkingTokensSpent``（幂等：同一轮只记一行）。

    world / life 收口用 :func:`app.agent.trace.collect_usage` 把 ``Agent.run`` 的本轮
    token 累下来后调这个 helper 落库。用 ``insert_idempotent``：整轮重试 / durable 重投
    用同一 ``(lane, actor, round_id)`` 再记一次无害（ON CONFLICT DO NOTHING、不重复计）。
    """
    await insert_idempotent(
        ThinkingTokensSpent(
            lane=lane,
            actor=actor,
            round_id=round_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            model_calls=model_calls,
            observed_at=observed_at,
        )
    )


async def record_round_cost(
    *,
    lane: str,
    actor: str,
    round_id: str,
    usage: dict[str, int],
    observed_at: str,
) -> None:
    """把一轮思考的 ``collect_usage`` 累计落 durable PG（PG insert best-effort，失败只 log 不抛）。

    world / life 收口共用的入口：把 :func:`app.agent.trace.collect_usage` yield 出来的
    累加 dict（input / output / total / cache_read_input_tokens / calls）映射成
    :func:`record_thinking_tokens` 的各 token 维度落库。``actor`` 由调用方填（world
    填 ``"world"``、life 填 persona_id）。

    **契约错误 fail-fast、旁路失败 best-effort —— 两者分开**：usage dict 的读取在
    ``try`` 之外，缺键直接抛 ``KeyError`` 让契约错误炸出来（usage 形态漂移 / 少键是
    真 bug，绝不能被当成"落库失败"静默吞掉、成本永远记不上却没人知道）。**只有** PG
    insert（:func:`record_thinking_tokens`）留在 ``try`` 里 best-effort 吞：成本观测是
    旁路，绝不能因为记成本失败把一轮真实思考 / 推演搞成失败重投（参考
    ``core._persist_session`` 的语义）。insert 失败只 log warning，调用方的后续收口
    （标已读 / 推进游标 / 排下次醒）照常进行。

    **已知 limitation（接受的取舍，不修）**：成本落库发生在 ``Agent.run`` 返回**之后**，
    而 turn 的 round marker 在 ``Agent.run`` 内部就已写进 transcript。usage 是 run 跑完
    才有的累计值、天然产生在 marker 之后，没法和 marker 塞进同一个幂等提交边界。所以若
    进程恰好崩在"run 返回后、``record_round_cost`` 调用前"这个窄窗口，重投会命中已写的
    marker 直接 skip → 那一轮的 ``ThinkingTokensSpent`` 永久漏记。这是 best-effort 观测
    的接受取舍：漏掉"崩溃恰好落在那个窄窗口"的极少数轮，远优于 langfuse 现在系统性丢
    durable 工具 trace 的 40-100%。**不要为把它纳入幂等边界改逻辑**——那是过度工程。
    """
    # usage 字典读取在 try 之外：缺键 = 契约错误，必须 fail-fast 抛 KeyError，不被吞。
    input_tokens = usage["input"]
    output_tokens = usage["output"]
    total_tokens = usage["total"]
    cached_tokens = usage["cache_read_input_tokens"]
    model_calls = usage["calls"]

    # 只有旁路 PG insert 留在 try 里 best-effort 吞：落库失败绝不拖垮一轮思考。
    try:
        await record_thinking_tokens(
            lane=lane,
            actor=actor,
            round_id=round_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            model_calls=model_calls,
            observed_at=observed_at,
        )
    except Exception as exc:  # noqa: BLE001 - cost observability must not fail a round
        logger.warning(
            "record token cost failed for %s/%s (round kept): %s",
            lane,
            actor,
            exc,
        )
