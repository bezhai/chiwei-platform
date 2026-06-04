"""Cron-tick dataflow Data classes — surface check.

旧 life tick / glimpse / daily-plan 的 Data 已在 world/life 重写中删除，对应
断言随之移除。这里只覆盖保留的 voice + light/heavy reviewer 调度信号。
"""
from __future__ import annotations

from app.runtime.data import key_fields


def test_all_classes_importable():
    from app.domain import life_dataflow as ld

    for name in [
        "MinuteTick", "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "VoiceRequest", "LightReviewRequest", "HeavyReviewRequest",
    ]:
        assert hasattr(ld, name), f"{name} missing"


def test_tick_classes_are_transient():
    from app.domain.life_dataflow import (
        HeavyReviewTick,
        LightDayTick,
        LightNightTick,
        MinuteTick,
    )
    for cls in [MinuteTick, LightDayTick, LightNightTick, HeavyReviewTick]:
        meta = getattr(cls, "Meta", None)
        assert meta is not None and getattr(meta, "transient", False), (
            f"{cls.__name__} should declare Meta.transient = True"
        )


def test_business_requests_keyed_by_persona():
    from app.domain.life_dataflow import (
        HeavyReviewRequest,
        LightReviewRequest,
        VoiceRequest,
    )
    for cls in [VoiceRequest, LightReviewRequest, HeavyReviewRequest]:
        assert key_fields(cls) == ("persona_id",), f"{cls.__name__} key wrong"


def test_deleted_classes_gone():
    """旧 life tick / glimpse / daily-plan 的 Data 必须已删干净。"""
    from app.domain import life_dataflow as ld

    for name in [
        "LifeTickRequest", "DailyPlanTick", "GlimpseTick", "GlimpseTickRequest",
        "SharedDailyContext", "DailyPlanRequest", "LifeStateChanged",
        "GlimpseRequest",
    ]:
        assert not hasattr(ld, name), f"{name} should have been deleted"
