"""Phase 4 dataflow — life engine / schedule / glimpse 调度信号 + 请求载荷.

cron tick 入口（5 种频率 + glimpse 5min）→ fan-out @node →
per-persona request → business @node。glimpse 还有一条 LifeStateChanged
即时事件路径，与 5min 周期路径汇入同一条 GlimpseRequest .durable() 边。

GlimpseRequest 是本期唯一持久化 Data —— durable 边要求消费端
``insert_idempotent`` dedup，必须有 pg 表。其他 Tick / Request 都是
进程内调度信号，``Meta.transient = True``。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


# ---------------------------------------------------------------------------
# Cron tick 入口
# ---------------------------------------------------------------------------


class MinuteTick(Data):
    """Per-minute cron source. Shared by life_tick + voice fan-out."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class LightDayTick(Data):
    """Light reviewer 白天节奏（每 30min, CST 8-21）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class LightNightTick(Data):
    """Light reviewer 夜间节奏（整点，CST 22-7 except 03）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class HeavyReviewTick(Data):
    """Heavy reviewer 每日节奏（CST 03:00）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class DailyPlanTick(Data):
    """Daily plan 每日节奏（CST 05:00）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class GlimpseTick(Data):
    """Glimpse 5min 周期节奏。"""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


# ---------------------------------------------------------------------------
# Per-persona business request
# ---------------------------------------------------------------------------


class LifeTickRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


class VoiceRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


class LightReviewRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str
    window_minutes: int

    class Meta:
        transient = True


class HeavyReviewRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


class GlimpseTickRequest(Data):
    """5min 周期 fan-out 出的 per-persona 触发；下游 glimpse_tick_node
    内部判 activity 决定是否 emit GlimpseRequest。"""
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


# ---------------------------------------------------------------------------
# Daily plan：shared pipeline 输出 + per-persona request（in-process 内存传递）
# ---------------------------------------------------------------------------


class SharedDailyContext(Data):
    """Daily plan shared pipeline 输出（wild agents + search + theater）。
    target_date 作 Key 让 graph 上是 per-day singleton。in-process only。"""
    target_date: Annotated[str, Key]   # YYYY-MM-DD
    wild_materials: str
    search_anchors: str
    theater: str

    class Meta:
        transient = True


class DailyPlanRequest(Data):
    persona_id: Annotated[str, Key]
    target_date: str
    wild_materials: str
    search_anchors: str
    theater: str

    class Meta:
        transient = True


# ---------------------------------------------------------------------------
# Glimpse 事件路径
# ---------------------------------------------------------------------------


class LifeStateChanged(Data):
    """commit_life_state_impl 写入成功后 emit。"""
    persona_id: Annotated[str, Key]
    activity_type: str
    prev_activity_type: str   # "" 表示首次提交
    ts: str

    class Meta:
        transient = True


class GlimpseRequest(Data):
    """走 .durable() 跨进程 → run_glimpse_node。

    request_id 是 emit 端生成的 uuid4 —— mq redelivery 时复用同一
    request_id 让 ``insert_idempotent`` 拒绝第二次插入，run_glimpse 不会
    被同一 request 跑两次。runtime 自动建 ``data_glimpse_request`` 表
    （migrator.py:76 命名规则 ``data_{to_snake(ClassName)}``）；副产品
    是 glimpse 触发的天然审计。

    没有 Meta.transient —— durable 边的硬约束（runtime graph.py:286 拒绝
    ``transient + .durable()`` 组合）。
    """
    request_id: Annotated[str, Key]   # uuid4
    persona_id: str
    chat_id: str
    ts: str
    trigger_kind: str   # "tick" | "event"，便于审计区分两路触发
