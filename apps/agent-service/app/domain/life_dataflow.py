"""Cron-tick dataflow Data — voice + light/heavy reviewer 调度信号 + 请求载荷.

cron tick 入口（每分钟 / light 白天夜间 / heavy）→ fan-out @node →
per-persona request → business @node。所有 Tick / Request 都是进程内调度信号，
``Meta.transient = True``。

旧 life tick / glimpse / daily-plan 的 Data 已在 world/life 重写中删除。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key

# ---------------------------------------------------------------------------
# Cron tick 入口
# ---------------------------------------------------------------------------


class MinuteTick(Data):
    """Per-minute cron source. Drives voice fan-out."""
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


# ---------------------------------------------------------------------------
# Per-persona business request
# ---------------------------------------------------------------------------


class VoiceRequest(Data):
    # persona_id default="" lets fan_out_voice @node emit a template
    # carrying only ts; the wire's ``.fan_out_per(...)`` then mutates
    # persona_id per-key via ``data.model_copy(update={...})``.
    persona_id: Annotated[str, Key] = ""
    ts: str

    class Meta:
        transient = True


class LightReviewRequest(Data):
    persona_id: Annotated[str, Key] = ""
    ts: str
    window_minutes: int

    class Meta:
        transient = True


class HeavyReviewRequest(Data):
    persona_id: Annotated[str, Key] = ""
    ts: str

    class Meta:
        transient = True
