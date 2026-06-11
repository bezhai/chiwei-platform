"""Cron-tick dataflow Data classes — surface check.

旧 life tick / glimpse / daily-plan 的 Data 已在 world/life 重写中删除；voice
子系统拆除后 MinuteTick / VoiceRequest 也随之删除。这里只覆盖保留的
light/heavy reviewer 调度信号。
"""
from __future__ import annotations

from app.runtime.data import key_fields


def test_all_classes_importable():
    from app.domain import life_dataflow as ld

    for name in [
        "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "LightReviewRequest", "HeavyReviewRequest",
    ]:
        assert hasattr(ld, name), f"{name} missing"


def test_tick_classes_are_transient():
    from app.domain.life_dataflow import (
        HeavyReviewTick,
        LightDayTick,
        LightNightTick,
    )
    for cls in [LightDayTick, LightNightTick, HeavyReviewTick]:
        meta = getattr(cls, "Meta", None)
        assert meta is not None and getattr(meta, "transient", False), (
            f"{cls.__name__} should declare Meta.transient = True"
        )


def test_business_requests_keyed_by_persona():
    from app.domain.life_dataflow import (
        HeavyReviewRequest,
        LightReviewRequest,
    )
    for cls in [LightReviewRequest, HeavyReviewRequest]:
        assert key_fields(cls) == ("persona_id",), f"{cls.__name__} key wrong"


def test_deleted_classes_gone():
    """旧 life tick / glimpse / daily-plan + voice 子系统的 Data 必须已删干净。"""
    from app.domain import life_dataflow as ld

    for name in [
        "LifeTickRequest", "DailyPlanTick", "GlimpseTick", "GlimpseTickRequest",
        "SharedDailyContext", "DailyPlanRequest", "LifeStateChanged",
        "GlimpseRequest",
        # voice 子系统拆除（MinuteTick 唯一用途是驱动 voice fan-out）
        "MinuteTick", "VoiceRequest",
    ]:
        assert not hasattr(ld, name), f"{name} should have been deleted"
